"""Klient Meta Conversions API (server-side eventy Purchase/Lead).

Kontrakt analityki (PLAN_LANDING "Decyzje Tomka 2026-06-10" + port-kontrakt-2):
- Eventy strzela WEBHOOK HANDLER (konwersja liczy sie bez powrotu przegladarki).
- event_id MUSI byc deterministyczny (deduplikacja z pikselem na froncie):
  Purchase -> payment_intent id albo invoice id; Lead -> sha256(email+data).
- email w user_data jest hashowany sha256 PO normalizacji (lower+trim).
- Brak META_CAPI_TOKEN / META_PIXEL_ID = klient wylaczony PO CICHU
  (send_event zwraca False; jeden WARN przy starcie - konwencja
  dev_mode.log_startup_mode). Blad API nigdy nie propaguje - analityka
  nie moze wywracac obslugi platnosci.
"""

import hashlib
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.logging import create_logger
from app.modules.admin.services import secrets, settings_catalog

log = create_logger("meta-capi")

META_API_VERSION = "v21.0"
META_API_BASE = "https://graph.facebook.com"
TIMEOUT_SECONDS = 10.0


def hash_email(email: str) -> str:
    """sha256 znormalizowanego emaila (wymog Meta dla pola em)."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CapiUserData:
    email: str | None = None  # surowy email - hashowanie robi klient
    fbc: str | None = None
    fbp: str | None = None
    client_ip: str | None = None
    client_ua: str | None = None


@dataclass(frozen=True)
class CapiCustomData:
    value: float | None = None  # kwota w PLN (nie grosze)
    currency: str | None = None  # "pln"
    content_name: str | None = None  # np. slug planu / "ebook"


def _pixel_id() -> str | None:
    """Efektywny Pixel ID (DB > env > brak). Niesekretny - sync accessor czyta
    procesowy cache ustawien; zimny cache = env fallback (1:1)."""
    return settings_catalog.effective_sync("metaPixelId")


def _capi_token() -> str | None:
    """Efektywny token CAPI (panel > env > brak). Sekret - czyta procesowy cache
    store sekretow, zimny cache schodzi na env (1:1)."""
    return secrets.resolve_sync("meta.capi_token", env_fallback=True)


def is_configured() -> bool:
    return _capi_token() is not None and _pixel_id() is not None


def _build_event(
    *,
    event_name: str,
    event_id: str,
    event_time: int,
    user_data: CapiUserData,
    custom_data: CapiCustomData | None,
    event_source_url: str | None,
    action_source: str,
) -> dict[str, Any]:
    ud: dict[str, Any] = {}
    if user_data.email:
        ud["em"] = [hash_email(user_data.email)]
    if user_data.fbc:
        ud["fbc"] = user_data.fbc
    if user_data.fbp:
        ud["fbp"] = user_data.fbp
    if user_data.client_ip:
        ud["client_ip_address"] = user_data.client_ip
    if user_data.client_ua:
        ud["client_user_agent"] = user_data.client_ua

    event: dict[str, Any] = {
        "event_name": event_name,
        "event_time": event_time,
        "event_id": event_id,
        "action_source": action_source,
        "user_data": ud,
    }
    if event_source_url is not None:
        event["event_source_url"] = event_source_url
    if custom_data is not None:
        cd: dict[str, Any] = {}
        if custom_data.value is not None:
            cd["value"] = custom_data.value
        if custom_data.currency is not None:
            cd["currency"] = custom_data.currency
        if custom_data.content_name is not None:
            cd["content_name"] = custom_data.content_name
        if cd:
            event["custom_data"] = cd
    return event


async def send_event(
    *,
    event_name: str,
    event_id: str,
    event_time: int,
    user_data: CapiUserData,
    custom_data: CapiCustomData | None = None,
    event_source_url: str | None = None,
    action_source: str = "website",
) -> bool:
    """Wysyla pojedynczy event do Meta CAPI. event_time = unix epoch (sekundy).

    Zwraca True gdy Meta przyjelo event; False gdy klient wylaczony albo
    wysylka padla (blad zalogowany, nie propagowany). Brak konfiguracji
    logowany raz przy starcie (dev_mode.log_startup_mode), nie tutaj.
    """
    if not is_configured():
        return False

    event = _build_event(
        event_name=event_name,
        event_id=event_id,
        event_time=event_time,
        user_data=user_data,
        custom_data=custom_data,
        event_source_url=event_source_url,
        action_source=action_source,
    )
    url = f"{META_API_BASE}/{META_API_VERSION}/{_pixel_id()}/events"
    payload = {"data": [event], "access_token": _capi_token()}

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload)
    except httpx.HTTPError as err:
        log.warn(f"Meta CAPI request failed for {event_name}/{event_id}: {err}")
        return False

    if response.status_code >= 300:
        log.warn(
            f"Meta CAPI error {response.status_code} for {event_name}/{event_id}: "
            f"{response.text[:300]}"
        )
        return False

    return True
