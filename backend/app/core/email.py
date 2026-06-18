"""Klient Resend (https://api.resend.com/emails) przez httpx.

Uzywany przez billing (maile transakcyjne) i newsletter (DOI, kontakt).
Polityka bledow nalezy do CALLERA: klient rzuca EmailConfigError /
EmailSendError, modul decyduje czy blad polyka (best-effort jak
send-contact-email) czy propaguje (request-cancellation -> 500).

Tryb mock (dev, app/core/dev_mode.py): zamiast Resend mail laduje jako plik
HTML w backend/.dev-outbox/ (naglowki w komentarzu na gorze pliku) - flow
DOI/anulowania/ebooka da sie klikac lokalnie i obejrzec maile w plikach.

Tu tez zyje normalize_email - obowiazkowa normalizacja (lower+trim)
KAZDEGO emaila wchodzacego do systemu (naprawa #5 z PLAN_LANDING).
"""

import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings
from app.core.dev_mode import resolve_mock
from app.core.logging import create_logger

log = create_logger("email")

RESEND_API_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "Be Free Club <noreply@befreeclub.pl>"
TIMEOUT_SECONDS = 10.0

# backend/.dev-outbox/ (gitignore) - mocki dev pisza tu maile.
OUTBOX_DIR = Path(__file__).resolve().parents[2] / ".dev-outbox"


def normalize_email(email: str) -> str:
    """lower + trim. Stosowac na KAZDYM wejsciu emaila (DB, Stripe, Circle)."""
    return email.strip().lower()


class EmailConfigError(Exception):
    """RESEND_API_KEY nie jest skonfigurowany."""


class EmailSendError(Exception):
    """Resend zwrocil blad albo request padl sieciowo."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


def is_mocked() -> bool:
    """Tryb mock dev: pliki w .dev-outbox/ zamiast requestow do Resend."""
    return resolve_mock(settings.MOCK_EMAIL, settings.RESEND_API_KEY is not None)


def is_configured() -> bool:
    """Czy maile da sie wysylac: prawdziwy klucz ALBO mock dev."""
    return settings.RESEND_API_KEY is not None or is_mocked()


def _slugify_subject(subject: str) -> str:
    value = subject.replace("ł", "l").replace("Ł", "L")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return value[:80] or "mail"


def _write_to_outbox(payload: dict[str, Any]) -> str:
    """Zapis maila do pliku HTML; naglowki w komentarzu, body renderowalne."""
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    filename = f"{timestamp}_{_slugify_subject(payload['subject'])}.html"
    header_lines = [
        f"From: {payload['from']}",
        f"To: {', '.join(payload['to'])}",
        f"Subject: {payload['subject']}",
    ]
    if "reply_to" in payload:
        header_lines.append(f"Reply-To: {payload['reply_to']}")
    for name, value in (payload.get("headers") or {}).items():
        header_lines.append(f"{name}: {value}")
    path = OUTBOX_DIR / filename
    path.write_text(
        "<!--\n" + "\n".join(header_lines) + "\n-->\n" + payload["html"], encoding="utf-8"
    )
    log.info(f"[MOCK] mail zapisany do {path}")
    return f"mock-{filename}"


async def send_email(
    *,
    to: str | list[str],
    subject: str,
    html: str,
    from_email: str | None = None,
    reply_to: str | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    """Wysyla maila przez Resend, zwraca id wiadomosci.

    from_email: None -> DEFAULT_FROM ("Be Free Club <noreply@befreeclub.pl>").
    headers: dodatkowe naglowki (np. X-Entity-Ref-ID w mailach DOI newslettera).
    """
    payload: dict[str, Any] = {
        "from": from_email or DEFAULT_FROM,
        "to": [to] if isinstance(to, str) else to,
        "subject": subject,
        "html": html,
    }
    if reply_to is not None:
        payload["reply_to"] = reply_to
    if headers:
        payload["headers"] = headers

    if is_mocked():
        return _write_to_outbox(payload)
    if settings.RESEND_API_KEY is None:
        raise EmailConfigError("RESEND_API_KEY is not configured")

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(
                RESEND_API_URL,
                json=payload,
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            )
    except httpx.HTTPError as err:
        raise EmailSendError(f"Resend request failed: {err}") from err

    if response.status_code >= 300:
        log.warn(f"Resend error {response.status_code}: {response.text[:300]}")
        raise EmailSendError(
            f"Resend returned {response.status_code}",
            status=response.status_code,
            body=response.text,
        )

    data = response.json()
    return str(data.get("id", ""))
