"""Klient Sender.net API v2 (port pushSubscriberToSender z newsletter-confirm).

Semantyka 1:1: POST /subscribers (trigger_automation: true), gdy non-OK
(np. subskrybent juz istnieje) fallback PATCH /subscribers/{email} z tym
samym body bez emaila. Oba nieudane -> False (handler zwraca 502).
Token defensywnie trimowany i zdejmowany z prefiksu "Bearer ".

Tryb mock (dev, app/core/dev_mode.py): payload tylko logowany (INFO), sukces.
"""

import re
from urllib.parse import quote

import httpx

from app.core.config import settings
from app.core.dev_mode import resolve_mock
from app.core.logging import create_logger
from app.modules.admin.services import secrets, settings_catalog

log = create_logger("newsletter:sender")

SENDER_API = "https://api.sender.net/v2"
DEFAULT_GROUP_IDS_CSV = "epnLzm,el06vl"
TIMEOUT_SECONDS = 10.0


def api_token() -> str:
    # Efektywny token (DB > env > ""). Sync accessor czyta procesowy cache
    # sekretow; zimny cache = env fallback. Jeden punkt -> is_mocked/
    # is_configured/push lapia wartosc z panelu po jej ustawieniu.
    raw = (secrets.resolve_sync("sender.api_token", env_fallback=True) or "").strip()
    return re.sub(r"^Bearer\s+", "", raw, flags=re.IGNORECASE)


def is_mocked() -> bool:
    """Tryb mock dev: push_subscriber tylko loguje payload i zwraca sukces."""
    return resolve_mock(settings.MOCK_SENDER, bool(api_token()))


def is_configured() -> bool:
    """Czy push do Sender.net zadziala: prawdziwy token ALBO mock dev."""
    return bool(api_token()) or is_mocked()


def group_ids() -> list[str]:
    # Efektywna wartosc (DB > env > default). Sync accessor czyta procesowy cache
    # ustawien; zimny cache = env fallback (zachowanie 1:1). Czytane per push.
    raw = settings_catalog.effective_sync("senderGroupIds") or DEFAULT_GROUP_IDS_CSV
    return [part.strip() for part in raw.split(",") if part.strip()]


async def push_subscriber(email: str, firstname: str) -> bool:
    """Upsert subskrybenta w Sender.net. True = sukces (create albo update)."""
    if is_mocked():
        log.info(
            "[MOCK] Sender.net push_subscriber",
            {
                "email": email,
                "firstname": firstname,
                "groups": group_ids(),
                "trigger_automation": True,
            },
        )
        return True

    headers = {
        "Authorization": f"Bearer {api_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    groups = group_ids()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, headers=headers) as client:
            created = await client.post(
                f"{SENDER_API}/subscribers",
                json={
                    "email": email,
                    "firstname": firstname,
                    "groups": groups,
                    "trigger_automation": True,
                },
            )
            if created.is_success:
                return True

            log.warn(
                "Sender create returned non-OK, attempting update: "
                f"{created.status_code} {created.text[:300]}"
            )

            # encodeURIComponent: poza domyslnymi quote() nie koduje !*'().
            encoded_email = quote(email, safe="!*'()")
            updated = await client.patch(
                f"{SENDER_API}/subscribers/{encoded_email}",
                json={
                    "firstname": firstname,
                    "groups": groups,
                    "trigger_automation": True,
                },
            )
            if not updated.is_success:
                log.error(f"Sender update failed: {updated.status_code} {updated.text[:300]}")
                return False
            return True
    except httpx.HTTPError as err:
        log.error(f"Sender request failed: {err}")
        return False
