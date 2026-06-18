"""Klient Circle Admin API v2 dla modulu members (provisioning/deprovisioning).

To NIE jest klient z circle_dm (tamten gada z Headless Member API). Members
uzywa Admin API v2: invite/remove/list czlonkow spolecznosci.

Semantyka 1:1 z edge functions (confirm-subscription, circle-cleanup,
sync-circle-ids, admin-reinvite-circle):
- invite: POST /api/admin/v2/community_members, 3 proby z backoffem 1s/2s,
  4xx (poza 429) bez retry; id czlonka z data.id LUB data.community_member.id
  (niespojnosc API Circle, kod sprawdza oba).
- remove: DELETE /api/admin/v2/community_members/{id}?community_id=...
  (endpoint admin v2 jak w circle-cleanup; oryginalny admin-pause uzywal
  nie-admin /api/v2/ - ujednolicone na admin v2). 404 = sukces (czlonka
  juz nie ma, cel osiagniety - zachowanie z admin-pause, naprawia petle
  retry cleanupu na nieistniejacym czlonku).
- list: GET ...?community_id&per_page=50&page=N, paginacja po has_next_page,
  sleep 200 ms miedzy stronami (1:1 sync-circle-ids).

Tryb mock (dev, app/core/dev_mode.py): fake in-memory - invite zwraca rosnace
id od 900001, remove sukces, find/list pusto, kazda akcja logowana. Dotyczy
TYLKO tego klienta; klient Circle w circle_dm NIGDY nie jest mockowany
(dev uzywa realnego Circle na kontach testowych - konwencja projektu).
"""

import asyncio
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.dev_mode import resolve_mock
from app.core.email import normalize_email
from app.core.logging import create_logger

log = create_logger("members.circle")

BASE = "https://app.circle.so"
TIMEOUT_SECONDS = 30.0
PAGE_SLEEP_SECONDS = 0.2

# Posrednio, zeby testy mogly podmienic bez realnego czekania.
_sleep = asyncio.sleep


class CircleConfigError(Exception):
    """Brak CIRCLE_API_TOKEN / CIRCLE_COMMUNITY_ID."""


class CircleApiError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Circle API error: {status}")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class InviteResult:
    ok: bool
    circle_member_id: str | None
    detail: str  # np. "Invited (status 200)" / "Circle API 422: ..."


_MOCK_FIRST_ID = 900001
_mock_state = {"next_id": _MOCK_FIRST_ID}


def reset_mock_state() -> None:
    """Reset licznika fake id (testy)."""
    _mock_state["next_id"] = _MOCK_FIRST_ID


def _has_credentials() -> bool:
    return bool(settings.CIRCLE_API_TOKEN) and bool(settings.CIRCLE_COMMUNITY_ID)


def is_mocked() -> bool:
    """Tryb mock dev: fake in-memory zamiast Circle Admin API."""
    return resolve_mock(settings.MOCK_CIRCLE_MEMBERS, _has_credentials())


def is_configured() -> bool:
    """Czy provisioning/cleanup zadziala: prawdziwy token+community ALBO mock dev."""
    return _has_credentials() or is_mocked()


def _credentials() -> tuple[str, str]:
    token = settings.CIRCLE_API_TOKEN
    community_id = settings.CIRCLE_COMMUNITY_ID
    if not token or not community_id:
        raise CircleConfigError("CIRCLE_API_TOKEN or CIRCLE_COMMUNITY_ID not set")
    return token, community_id


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _extract_member_id(data: dict) -> str | None:
    """data.id?.toString() || data.community_member?.id?.toString() || null."""
    member_id = data.get("id")
    if member_id is None and isinstance(data.get("community_member"), dict):
        member_id = data["community_member"].get("id")
    return str(member_id) if member_id is not None else None


async def invite(email: str, *, skip_invitation: bool = False, retries: int = 3) -> InviteResult:
    """Zaproszenie do spolecznosci. NIE rzuca - brak configu / porazka = ok=False.

    1:1 inviteToCircle z confirm-subscription: retry tylko na 5xx/429/wyjatek,
    backoff 1000ms * numer proby.
    """
    if is_mocked():
        fake_id = str(_mock_state["next_id"])
        _mock_state["next_id"] += 1
        log.info(
            f"[MOCK] Circle invite {email} -> fake member id {fake_id}"
            f" (skip_invitation={skip_invitation})"
        )
        return InviteResult(ok=True, circle_member_id=fake_id, detail="Mock invite (dev)")
    if not is_configured():
        log.error("Missing CIRCLE_API_TOKEN or CIRCLE_COMMUNITY_ID")
        return InviteResult(ok=False, circle_member_id=None, detail="Circle credentials not configured")
    token, community_id = _credentials()

    last_detail = ""
    for attempt in range(1, retries + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{BASE}/api/admin/v2/community_members",
                    headers=_headers(token),
                    json={
                        "community_id": int(community_id),
                        "email": email,
                        "skip_invitation": skip_invitation,
                    },
                )
            if not response.is_success:
                last_detail = f"Circle API {response.status_code}: {response.text[:300]}"
                log.error(f"Invite failed for {email} (attempt {attempt}/{retries})", last_detail)
                # 4xx poza 429 - bez retry (blad klienta, ponowienie nic nie da)
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    return InviteResult(ok=False, circle_member_id=None, detail=last_detail)
                if attempt < retries:
                    await _sleep(attempt)
                    continue
                return InviteResult(ok=False, circle_member_id=None, detail=last_detail)
            member_id = _extract_member_id(response.json())
            return InviteResult(
                ok=True,
                circle_member_id=member_id,
                detail=f"Invited (status {response.status_code})",
            )
        except Exception as err:  # siec/JSON - jak catch w TS: retry albo porazka
            last_detail = f"Exception: {err}"
            log.error(f"Invite error for {email} (attempt {attempt}/{retries})", str(err))
            if attempt < retries:
                await _sleep(attempt)
                continue
    return InviteResult(ok=False, circle_member_id=None, detail=last_detail)


async def remove(circle_member_id: str) -> bool:
    """Usuniecie czlonka ze spolecznosci. True = usuniety albo juz go nie bylo (404)."""
    if is_mocked():
        log.info(f"[MOCK] Circle remove member {circle_member_id} -> ok")
        return True
    token, community_id = _credentials()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.delete(
                f"{BASE}/api/admin/v2/community_members/{circle_member_id}"
                f"?community_id={community_id}",
                headers=_headers(token),
            )
    except httpx.HTTPError as err:
        log.error(f"Failed to remove member {circle_member_id}", str(err))
        return False
    if response.is_success:
        return True
    if response.status_code == 404:
        log.info(f"Member {circle_member_id} already absent in Circle (404), treating as removed")
        return True
    log.error(
        f"Failed to remove member {circle_member_id}: "
        f"[{response.status_code}] {response.text[:300]}"
    )
    return False


async def fetch_all_members() -> dict[str, str]:
    """Wszyscy czlonkowie spolecznosci: mapa lowercase(email) -> circle_member_id.

    Paginacja per_page=50 po has_next_page, 200 ms przerwy miedzy stronami
    (1:1 sync-circle-ids).
    """
    if is_mocked():
        log.info("[MOCK] Circle fetch_all_members -> pusto")
        return {}
    token, community_id = _credentials()
    mapping: dict[str, str] = {}
    page = 1
    has_next_page = True
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        while has_next_page:
            response = await client.get(
                f"{BASE}/api/admin/v2/community_members"
                f"?community_id={community_id}&per_page=50&page={page}",
                headers=_headers(token),
            )
            if not response.is_success:
                raise CircleApiError(response.status_code, response.text)
            data = response.json()
            records = data.get("records") or []
            for record in records:
                if record.get("email") and record.get("id"):
                    mapping[normalize_email(record["email"])] = str(record["id"])
            has_next_page = data.get("has_next_page") is True
            page += 1
            log.info(f"Fetched page {page - 1}, got {len(records)} members (total: {len(mapping)})")
            await _sleep(PAGE_SLEEP_SECONDS)
    return mapping


async def find_member_id_by_email(email: str) -> str | None:
    """Szuka czlonka po emailu (paginacja per_page=50, pierwsze trafienie konczy)."""
    if is_mocked():
        log.info(f"[MOCK] Circle find_member_id_by_email {email} -> None")
        return None
    token, community_id = _credentials()
    needle = normalize_email(email)
    page = 1
    has_next_page = True
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        while has_next_page:
            response = await client.get(
                f"{BASE}/api/admin/v2/community_members"
                f"?community_id={community_id}&per_page=50&page={page}",
                headers=_headers(token),
            )
            if not response.is_success:
                raise CircleApiError(response.status_code, response.text)
            data = response.json()
            for record in data.get("records") or []:
                record_email = record.get("email")
                if record_email and normalize_email(record_email) == needle:
                    record_id = record.get("id")
                    return str(record_id) if record_id is not None else None
            has_next_page = data.get("has_next_page") is True
            page += 1
            await _sleep(PAGE_SLEEP_SECONDS)
    return None
