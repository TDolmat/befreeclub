"""Port flow anulowania (request-cancellation + confirm-cancellation).

Spec: billing-lifecycle.md sekcja 3. Semantyka i mail 1:1 z naprawami:
- magic link HMAC (CANCELLATION_DOI_SECRET, exp +60 min, reason 1..60 znakow
  w payloadzie) - tresc maila DOSLOWNIE z request-cancellation/index.ts,
- token JEDNORAZOWY (rejestr zuzytych w services/magic_link.py) - naprawa
  "wielokrotnego uzytku" z zadania; potwierdzenie wymaga jawnego POST z body
  (skanery poczty robia GET/prefetch - nie anuluja za usera),
- wpis cancellation_reasons ZAWSZE (reason z payloadu albo "not-given") -
  kontrakt #11, panel potrzebuje pelnej historii (oryginal gubil anulowania
  bez powodu),
- token zmiany karty (payload z purpose) NIE anuluje subskrypcji,
- bledy jako HTTP 4xx zamiast 200+{"error"} (kontrakt 1.1), komunikaty PL 1:1.
"""

import time
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import email as email_core
from app.core.config import settings
from app.core.email import DEFAULT_FROM, EmailConfigError, EmailSendError, normalize_email
from app.core.logging import create_logger, to_iso_string
from app.core.stripe_client import get_client
from app.modules.admin.services import settings_catalog
from app.modules.billing.models import CancellationReason
from app.modules.billing.services import magic_link
from app.modules.billing.services.subscriptions import (
    CANCELLABLE_STATUSES,
    find_subscriptions_by_email,
    obj_get,
    period_end_ts,
)

log = create_logger("billing.cancel")

DEFAULT_FRONTEND_URL = "https://befreeclub.pl"
TOKEN_TTL_MS = 60 * 60 * 1000  # link wazny 60 minut

# Komunikaty PL 1:1 z oryginalu (bajt w bajt).
EMAIL_REQUIRED_MESSAGE = "Email jest wymagany"
NOT_FOUND_REQUEST_MESSAGE = "Nie znaleziono aktywnej subskrypcji dla tego adresu email."
NOT_FOUND_CONFIRM_MESSAGE = "Nie znaleziono aktywnej subskrypcji do anulowania."
INVALID_TOKEN_MESSAGE = (
    "Link wygasł lub jest nieprawidłowy. Wróć na stronę anulowania i wyślij nowy."
)
MISSING_TOKEN_MESSAGE = "Brak tokenu"
EMAIL_SEND_FAILED_MESSAGE = "Nie udało się wysłać emaila potwierdzającego"

EMAIL_SUBJECT = "Potwierdź anulowanie subskrypcji Be Free Club"
EMAIL_REPLY_TO = "kontakt@befreeclub.pl"


def _escape_html(s: str) -> str:
    """1:1 escapeHtml z oryginalu (kolejnosc & < > ")."""
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


# Tresc HTML 1:1 z request-cancellation/index.ts (buildEmailHtml) -
# jedyna zmiana: ${safeUrl} -> placeholder __SAFE_URL__.
_EMAIL_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<title>Potwierdź anulowanie subskrypcji</title>
<style>
  :root { color-scheme: light only; supported-color-schemes: light only; }
  @media (prefers-color-scheme: dark) {
    .bfc-card { background:#2c2d31 !important; }
    .bfc-bg { background:#1a1b1f !important; }
    .bfc-btn { background:#ECE183 !important; }
    .bfc-btn-link { color:#1a1b1f !important; }
    .bfc-h1 { color:#ffffff !important; }
    .bfc-body { color:#cfd1d4 !important; }
    .bfc-muted { color:#a8aab0 !important; }
    .bfc-faint { color:#666 !important; }
    .bfc-eyebrow { color:#999 !important; }
    .bfc-brand { color:#ECE183 !important; }
  }
</style>
</head>
<body class="bfc-bg" style="margin:0;padding:0;background:#1a1b1f;font-family:'Space Grotesk','Inter',Arial,sans-serif;-webkit-text-size-adjust:100%;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="bfc-bg" style="background:#1a1b1f;">
    <tr><td align="center" style="padding:40px 16px;">
      <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" class="bfc-card" style="max-width:560px;width:100%;background:#2c2d31;border-radius:16px;border:1px solid #3a3b3f;">
        <tr><td style="padding:40px 32px;">
          <p class="bfc-eyebrow" style="font-size:12px;color:#999;letter-spacing:1px;text-transform:uppercase;margin:0 0 20px;font-family:Arial,sans-serif;">Anulowanie subskrypcji <span class="bfc-brand" style="color:#ECE183;">Be Free Club</span></p>
          <h1 class="bfc-h1" style="font-size:24px;font-weight:700;line-height:1.3;color:#ffffff;margin:0 0 20px;font-family:'Space Grotesk',Arial,sans-serif;">
            Potwierdź anulowanie subskrypcji
          </h1>
          <p class="bfc-body" style="font-size:16px;line-height:1.6;color:#cfd1d4;margin:0 0 14px;">
            Otrzymaliśmy prośbę o anulowanie Twojej subskrypcji. Kliknij poniższy przycisk, żeby ją potwierdzić.
          </p>
          <p class="bfc-body" style="font-size:16px;line-height:1.6;color:#cfd1d4;margin:0 0 28px;">
            Zachowasz pełen dostęp do klubu do końca opłaconego okresu. Po nim subskrypcja wygaśnie i nie zostaniesz obciążony kolejną opłatą.
          </p>
          <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 28px;"><tr><td align="center" class="bfc-btn" bgcolor="#ECE183" style="border-radius:12px;background:#ECE183;">
            <a href="__SAFE_URL__" class="bfc-btn-link" style="display:inline-block;padding:14px 32px;font-size:16px;font-weight:700;color:#1a1b1f;text-decoration:none;font-family:'Space Grotesk',Arial,sans-serif;border-radius:12px;">
              Potwierdzam anulowanie
            </a>
          </td></tr></table>
          <p class="bfc-muted" style="font-size:13px;line-height:1.6;color:#888;margin:0;">
            Link wygasa za 60 minut. Jeśli to nie Ty zainicjowałeś anulowanie, zignoruj ten mail. Nic się nie zmieni.
          </p>
          <p class="bfc-faint" style="font-size:12px;line-height:1.6;color:#a8aab0;margin:24px 0 0;border-top:1px solid #3a3b3f;padding-top:16px;font-family:Arial,sans-serif;">
            Nie działa przycisk? Skopiuj ten link do przeglądarki:<br>
            <a href="__SAFE_URL__" class="bfc-brand" style="color:#ECE183 !important;text-decoration:underline;word-break:break-all;">__SAFE_URL__</a>
          </p>
        </td></tr>
      </table>
      <p class="bfc-faint" style="font-size:12px;color:#666;margin:24px 0 0;font-family:Arial,sans-serif;">
        <a href="https://befreeclub.pl/" class="bfc-brand" style="color:#ECE183;text-decoration:none;">Be Free Club</a> &middot; <a href="https://www.instagram.com/krystianbefree/" class="bfc-brand" style="color:#ECE183;text-decoration:none;">Krystian Rudnik</a>
      </p>
    </td></tr>
  </table>
</body></html>"""


def build_email_html(confirm_url: str) -> str:
    return _EMAIL_HTML_TEMPLATE.replace("__SAFE_URL__", _escape_html(confirm_url))


def _require_doi_secret() -> str:
    secret = settings.CANCELLATION_DOI_SECRET
    if not secret:
        raise HTTPException(500, "CANCELLATION_DOI_SECRET not set")
    return secret


# ── request-cancellation -> POST /cancellation/request ───────────────────────


async def request_cancellation(*, email: str | None, reason: str | None) -> dict:
    """Szuka aktywnej suby na OBU kontach i wysyla magic link na maila.

    UWAGA (1:1 z oryginalem): odpowiedz 404 zdradza, czy email ma subskrypcje
    (enumeracja) - swiadomie zachowane, bo front pokazuje ten komunikat
    userowi na /anuluj. Rate limit na route ogranicza skan.
    """
    if not email or not isinstance(email, str):
        raise HTTPException(400, EMAIL_REQUIRED_MESSAGE)
    email = normalize_email(email)

    secret = _require_doi_secret()
    if not email_core.is_configured():
        raise HTTPException(500, "RESEND_API_KEY not set")

    found = await find_subscriptions_by_email(email, statuses=CANCELLABLE_STATUSES)
    if not found:
        raise HTTPException(404, NOT_FOUND_REQUEST_MESSAGE)
    log.info(f"Found active sub for {email}, sending magic link")

    payload: dict = {"email": email, "exp": int(time.time() * 1000) + TOKEN_TTL_MS}
    if isinstance(reason, str) and 1 <= len(reason) <= 60:
        # Dluzszy powod jest po cichu gubiony - 1:1 z oryginalem.
        payload["reason"] = reason
    token = magic_link.sign_token(payload, secret)

    # Efektywne wartosci (DB > env > default). Sciezka async, wiec await.
    frontend = await settings_catalog.effective("frontendUrl") or DEFAULT_FRONTEND_URL
    confirm_url = f"{frontend}/anuluj/potwierdz?token={quote(token, safe='')}"

    try:
        await email_core.send_email(
            to=email,
            subject=EMAIL_SUBJECT,
            html=build_email_html(confirm_url),
            from_email=await settings_catalog.effective("cancellationFromEmail") or DEFAULT_FROM,
            reply_to=EMAIL_REPLY_TO,
        )
    except (EmailConfigError, EmailSendError) as err:
        log.error(f"Resend error: {err}")
        raise HTTPException(502, EMAIL_SEND_FAILED_MESSAGE) from err

    log.info(f"Magic link sent to {email}")
    return {"success": True}


# ── confirm-cancellation -> POST /cancellation/confirm ───────────────────────


async def confirm_cancellation(session: AsyncSession, *, token: str | None) -> dict:
    """cancel_at_period_end=true na WSZYSTKICH pasujacych subach obu kont
    (dostep do konca oplaconego okresu - NIE kasuje od razu). Token
    jednorazowy: claim ATOMOWO przed operacjami Stripe (review 2.1 - rozdzial
    is_used/mark_used przez awaity pozwalal dwom rownoleglym confirmom
    wstawic duplikat audytu); sciezki bez skutku (404, blad Stripe) oddaja
    token przez release."""
    if not token or not isinstance(token, str):
        raise HTTPException(400, MISSING_TOKEN_MESSAGE)
    secret = _require_doi_secret()

    payload = magic_link.verify_token(token, secret)
    # purpose w payloadzie = token innego flow (zmiana karty) - nie anuluje.
    if payload is None or payload.get("purpose") is not None:
        raise HTTPException(410, INVALID_TOKEN_MESSAGE)
    if not magic_link.claim(token, exp_ms=payload["exp"]):
        raise HTTPException(410, INVALID_TOKEN_MESSAGE)

    email = normalize_email(str(payload["email"]))
    raw_reason = payload.get("reason")
    reason = raw_reason if isinstance(raw_reason, str) and raw_reason else None

    try:
        cancelled = 0
        end_date: str | None = None
        for item in await find_subscriptions_by_email(email, statuses=CANCELLABLE_STATUSES):
            sub = item.subscription
            updated = sub
            if not obj_get(sub, "cancel_at_period_end"):
                client = get_client(item.account)
                updated = await client.v1.subscriptions.update_async(
                    sub.id, params={"cancel_at_period_end": True}
                )
            cancelled += 1
            if end_date is None:
                end_ts = period_end_ts(updated, include_cancel_at=True)
                if end_ts is not None:
                    end_date = to_iso_string(datetime.fromtimestamp(end_ts, UTC))
            log.info(f"Scheduled cancellation for sub {sub.id} ({email})")

        if cancelled == 0:
            # Tokenu nie zuzywamy - user moze sprobowac ponownie w oknie exp.
            magic_link.release(token)
            raise HTTPException(404, NOT_FOUND_CONFIRM_MESSAGE)

        # Wpis ZAWSZE (kontrakt #11) - oryginal gubil anulowania bez powodu.
        session.add(
            CancellationReason(email=email, reason=reason or "not-given", action="cancelled")
        )
        await session.commit()
    except HTTPException:
        raise
    except Exception:
        # Blad Stripe/DB bez pelnego skutku - token wraca do uzycia.
        magic_link.release(token)
        raise

    log.info(f"Cancelled {cancelled} sub(s) for {email}")
    return {"success": True, "cancelled": cancelled, "access_until": end_date}
