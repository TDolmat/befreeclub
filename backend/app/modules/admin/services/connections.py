"""Status polaczen z zewnetrznymi API dla panelu admina (sekcja "Polaczenia API").

ZELAZNA ZASADA: NIGDY nie zwracamy, nie zapisujemy ani nie logujemy wartosci
sekretu (ani jego fragmentu). Panel widzi tylko:
- `configured`: czy sekret jest ustawiony w env (bool),
- `status`: 'ok' | 'error' | 'unconfigured' | 'skipped' | 'mock',
- `detail`: krotki, BEZPIECZNY opis (bez sekretu).

`status` liczymy z istniejacych helperow `is_configured()` danego serwisu, bez
odczytu wartosci. "Test polaczenia" robi minimalne read-only zapytanie do
dostawcy z krotkim timeoutem; kazdy blad jest lapany i mapowany na
status 'error' z detalem bez sekretu. Tam gdzie nie ma taniego bezpiecznego
testu (Resend/Sender/Meta) status liczymy z samej obecnosci klucza i zwracamy
detail "brak test-call".

Tryb dev/mock (app/core/dev_mode.py): brak klucza poza produkcja = status
'mock' (serwis dziala na mocku), a nie 'error'/500.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
import stripe

from app.core import email, meta_capi
from app.core.config import settings
from app.core.dev_mode import is_production
from app.core.logging import create_logger
from app.core.stripe_client import (
    StripeAccount,
    StripeConfigError,
    get_client,
)
from app.core.stripe_client import (
    is_configured as stripe_is_configured,
)
from app.modules.admin.services import secrets
from app.modules.members.services import circle as members_circle
from app.modules.newsletter.services import sender

log = create_logger("admin.connections")

# Krotki timeout testow - panel nie moze wisiec na padajacym dostawcy.
TEST_TIMEOUT_S = 8.0

CIRCLE_ADMIN_BASE = "https://app.circle.so"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
RESEND_DOMAINS_URL = "https://api.resend.com/domains"


@dataclass(frozen=True)
class ConnectionResult:
    key: str
    label: str
    configured: bool
    status: str  # 'ok' | 'error' | 'unconfigured' | 'skipped' | 'mock'
    detail: str
    # Bezpieczne domysle: nawet gdyby ktore miejsce zapomnialo je podac, strona
    # Polaczen sie nie wywroci (500) - pokaze status-only z env.
    source: str = "brak"  # 'panel' | 'env' | 'brak' (zrodlo efektywnej wartosci)
    editable: bool = False  # czy klucz da sie ustawic w panelu (4 sekrety) czy status-only
    masked: str | None = None  # zamaskowana EFEKTYWNA wartosc (tylko editable), nigdy pelna

    def to_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "configured": self.configured,
            "status": self.status,
            "detail": self.detail,
            "source": self.source,
            "editable": self.editable,
            "masked": self.masked,
        }


@dataclass(frozen=True)
class _ConnectionDef:
    key: str
    label: str
    is_configured: Callable[[], bool]
    # Test zwraca (ok, detail). None = brak taniego/bezpiecznego testu.
    test: Callable[[], Awaitable[tuple[bool, str]]] | None
    # Czy serwis ma tryb mock dev (brak klucza poza prod = dziala na mocku).
    is_mocked: Callable[[], bool] | None = None
    no_test_detail: str = "brak test-call"
    # store_key sekretu (4 edytowalne). None = status-only z env (stripe/circle).
    secret_key: str | None = None
    # Nazwa pola env z fallbackiem (dla source/effective_value). None gdy nieedytowalny.
    env_attr: str | None = None


# Wzorce sekretow do wymazania z komunikatow bledow PRZED pokazaniem ich w UI.
# Lapiemy znane prefiksy kluczy (Stripe sk_/rk_/whsec_, Resend re_), naglowek
# Bearer oraz dlugie ciagi hex/base64 (>=20 znakow) wygladajace jak token.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]+"),
    re.compile(r"whsec_[A-Za-z0-9]+"),
    re.compile(r"re_[A-Za-z0-9_]+"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"[A-Za-z0-9+/=_\-]{20,}"),
)


def _scrub_secrets(text: str) -> str:
    """Wymazuje znane wzorce sekretow z tekstu, zastepujac je '[redacted]'."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _safe_error_detail(prefix: str, err: Exception) -> str:
    """Krotki opis bledu BEZ ryzyka wycieku sekretu.

    Komunikaty httpx/SDK moga zawierac fragment klucza (np. Stripe echo'uje
    prefiks i 4 ostatnie znaki klucza) albo token w URL/naglowku. Najpierw
    SCRUBUJEMY znane wzorce sekretow, dopiero potem obcinamy do limitu - dzieki
    temu funkcja broni niezaleznie od tresci wejscia.
    """
    text = _scrub_secrets(str(err).strip())
    if len(text) > 200:
        text = text[:200] + "…"
    return f"{prefix}: {text}" if text else prefix


# ── Testy per dostawca (read-only, tani authed call) ──────────────────────


async def _test_stripe(account: StripeAccount) -> tuple[bool, str]:
    try:
        client = get_client(account)
    except StripeConfigError:
        return False, "brak klucza w env"
    try:
        # Najtanszy authed read: GET /v1/balance (bez listowania zasobow).
        # Timeout na poziomie SDK nie jest per-request (HTTPXClient ma wlasny),
        # wiec krotki budzet egzekwujemy zewnetrznie przez wait_for.
        await asyncio.wait_for(client.v1.balance.retrieve_async(), timeout=TEST_TIMEOUT_S)
    except TimeoutError:
        return False, "timeout"
    except stripe.error.StripeError as err:
        # str(StripeError) to komunikat API i ZAWIERA fragment klucza: Stripe
        # echo'uje prefiks + 4 ostatnie znaki ("Invalid API Key provided:
        # sk_test_****wxyz"). NIE oddajemy str(err) - mapujemy typ wyjatku na
        # stale, bezpieczne stringi spojne z reszta dostawcow (f"HTTP {code}").
        return False, _stripe_error_detail(err)
    except Exception as err:  # noqa: BLE001 - nie-Stripe blad (np. httpx), status error
        return False, _safe_error_detail(type(err).__name__, err)
    return True, "GET /v1/balance ok"


def _stripe_error_detail(err: stripe.error.StripeError) -> str:
    """Mapuje wyjatek SDK Stripe na BEZPIECZNY, staly string (nigdy str(err)).

    str(StripeError) zawiera fragment klucza, wiec go nie dotykamy. Bierzemy
    tylko typ wyjatku, ewentualnie err.http_status (whitelist liczb), zeby
    front rozpoznal auth (matcher 'http 401').
    """
    if isinstance(err, stripe.error.AuthenticationError):
        return "HTTP 401"
    if isinstance(err, stripe.error.PermissionError):
        return "HTTP 403"
    status = getattr(err, "http_status", None)
    if isinstance(status, int):
        return f"HTTP {status}"
    return "test padl"


async def _test_stripe_current() -> tuple[bool, str]:
    return await _test_stripe(StripeAccount.CURRENT)


async def _test_stripe_legacy() -> tuple[bool, str]:
    return await _test_stripe(StripeAccount.LEGACY)


async def _test_circle() -> tuple[bool, str]:
    """Tani authed call do Circle Admin API v2: lista 1 czlonka.

    Uzywa tego samego tokenu/community co provisioning (members.circle),
    wiec test pokrywa to, co realnie liczy circle.is_configured().
    """
    token = settings.CIRCLE_API_TOKEN
    # community_id niesekretne: efektywna wartosc (DB > env), zgodnie z provisioning.
    community_id = members_circle._community_id()
    if not token or not community_id:
        return False, "brak CIRCLE_API_TOKEN / CIRCLE_COMMUNITY_ID"
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT_S) as client:
            response = await client.get(
                f"{CIRCLE_ADMIN_BASE}/api/admin/v2/community_members"
                f"?community_id={community_id}&per_page=1&page=1",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as err:
        return False, _safe_error_detail("blad sieci", err)
    if response.is_success:
        return True, "GET /community_members ok"
    return False, f"HTTP {response.status_code}"


async def _test_openai() -> tuple[bool, str]:
    """GET /v1/models - najtanszy authed read OpenAI. Klucz efektywny (DB > env)."""
    api_key = await secrets.resolve("openai.api_key", env_fallback=True)
    if not api_key:
        return False, "brak klucza"
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT_S) as client:
            response = await client.get(
                OPENAI_MODELS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as err:
        return False, _safe_error_detail("blad sieci", err)
    if response.is_success:
        return True, "GET /v1/models ok"
    return False, f"HTTP {response.status_code}"


async def _test_resend() -> tuple[bool, str]:
    """GET /domains - tani authed read Resend. Klucz efektywny (DB > env)."""
    api_key = await secrets.resolve("resend.api_key", env_fallback=True)
    if not api_key:
        return False, "brak klucza"
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT_S) as client:
            response = await client.get(
                RESEND_DOMAINS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as err:
        return False, _safe_error_detail("blad sieci", err)
    if response.is_success:
        return True, "GET /domains ok"
    return False, f"HTTP {response.status_code}"


# ── Rejestr polaczen ──────────────────────────────────────────────────────


def _stripe_current_configured() -> bool:
    return stripe_is_configured(StripeAccount.CURRENT)


def _stripe_legacy_configured() -> bool:
    return stripe_is_configured(StripeAccount.LEGACY)


def _circle_configured() -> bool:
    return bool(settings.CIRCLE_API_TOKEN) and bool(members_circle._community_id())


def _openai_configured() -> bool:
    # Efektywny klucz (cache DB > env). Po set_secret cache jest ogrzany.
    return secrets.resolve_sync("openai.api_key", env_fallback=True) is not None


def _resend_configured() -> bool:
    return secrets.resolve_sync("resend.api_key", env_fallback=True) is not None


def _sender_configured() -> bool:
    return bool(sender.api_token())


CONNECTIONS: tuple[_ConnectionDef, ...] = (
    _ConnectionDef(
        key="stripeCurrent",
        label="Stripe (konto current)",
        is_configured=_stripe_current_configured,
        test=_test_stripe_current,
    ),
    _ConnectionDef(
        key="stripeLegacy",
        label="Stripe (konto legacy)",
        is_configured=_stripe_legacy_configured,
        test=_test_stripe_legacy,
    ),
    _ConnectionDef(
        key="circle",
        label="Circle API",
        is_configured=_circle_configured,
        test=_test_circle,
        is_mocked=members_circle.is_mocked,
    ),
    _ConnectionDef(
        key="openai",
        label="OpenAI (STT/vision)",
        is_configured=_openai_configured,
        test=_test_openai,
        secret_key="openai.api_key",
        env_attr="OPENAI_API_KEY",
    ),
    _ConnectionDef(
        key="resend",
        label="Resend (maile)",
        is_configured=_resend_configured,
        test=_test_resend,
        is_mocked=email.is_mocked,
        secret_key="resend.api_key",
        env_attr="RESEND_API_KEY",
    ),
    _ConnectionDef(
        key="sender",
        label="Sender.net (newsletter)",
        is_configured=_sender_configured,
        test=None,
        is_mocked=sender.is_mocked,
        secret_key="sender.api_token",
        env_attr="SENDER_API_TOKEN",
    ),
    _ConnectionDef(
        key="metaCapi",
        label="Meta Conversions API",
        is_configured=meta_capi.is_configured,
        test=None,
        secret_key="meta.capi_token",
        env_attr="META_CAPI_TOKEN",
    ),
)

# connection_key -> store_key, tylko 4 edytowalne. Mapowanie dla set/clear/reveal.
_SECRET_KEY_BY_CONNECTION: dict[str, str] = {
    c.key: c.secret_key for c in CONNECTIONS if c.secret_key is not None
}

_BY_KEY: dict[str, _ConnectionDef] = {c.key: c for c in CONNECTIONS}


async def _meta(conn: _ConnectionDef) -> tuple[str, bool, str | None]:
    """Liczy (source, editable, masked) dla integracji.

    Edytowalne (4 sekrety): source = secret_status (DB row > env > brak), masked =
    mask(effective_value) (zamaskowana EFEKTYWNA wartosc, nigdy pelna). Nieedytowalne
    (stripe/circle): editable=False, masked=None, source z samej obecnosci env."""
    if conn.secret_key is None:
        # status-only z env: source 'env' gdy klucz w env, inaczej 'brak'.
        source = "env" if conn.is_configured() else "brak"
        return source, False, None
    source = await secrets.secret_status(conn.secret_key, env_fallback=True)
    masked = mask(await secrets.effective_value(conn.secret_key, env_fallback=True))
    return source, True, masked


def mask(v: str | None) -> str | None:
    """Re-export maski sekretu (jedno zrodlo: services.secrets.mask)."""
    return secrets.mask(v)


def _mock_result(
    conn: _ConnectionDef, *, source: str, editable: bool, masked: str | None
) -> ConnectionResult:
    return ConnectionResult(
        key=conn.key,
        label=conn.label,
        configured=False,
        status="mock",
        detail="dev: brak klucza, serwis dziala na mocku",
        source=source,
        editable=editable,
        masked=masked,
    )


async def _evaluate(conn: _ConnectionDef, *, run_test: bool) -> ConnectionResult:
    source, editable, masked = await _meta(conn)
    configured = conn.is_configured()

    # Dev: brak klucza, ale serwis ma mock -> 'mock', nie 'error'/'unconfigured'.
    if not configured and conn.is_mocked is not None and not is_production() and conn.is_mocked():
        return _mock_result(conn, source=source, editable=editable, masked=masked)

    if not configured:
        return ConnectionResult(
            key=conn.key,
            label=conn.label,
            configured=False,
            status="unconfigured",
            detail="brak klucza",
            source=source,
            editable=editable,
            masked=masked,
        )

    if conn.test is None:
        return ConnectionResult(
            key=conn.key,
            label=conn.label,
            configured=True,
            status="skipped",
            detail=conn.no_test_detail,
            source=source,
            editable=editable,
            masked=masked,
        )

    if not run_test:
        return ConnectionResult(
            key=conn.key,
            label=conn.label,
            configured=True,
            status="skipped",
            detail="test nie uruchomiony",
            source=source,
            editable=editable,
            masked=masked,
        )

    try:
        ok, detail = await conn.test()
    except Exception as err:  # noqa: BLE001 - nigdy nie pozwol testowi wywrocic endpointu
        log.warn(f"connection test {conn.key} crashed", type(err).__name__)
        return ConnectionResult(
            key=conn.key,
            label=conn.label,
            configured=True,
            status="error",
            detail=_safe_error_detail("test padl", err),
            source=source,
            editable=editable,
            masked=masked,
        )
    return ConnectionResult(
        key=conn.key,
        label=conn.label,
        configured=True,
        status="ok" if ok else "error",
        detail=detail,
        source=source,
        editable=editable,
        masked=masked,
    )


async def list_connections(*, run_tests: bool = False) -> list[ConnectionResult]:
    """Status wszystkich polaczen. `run_tests=False` = tani listing (sam status
    z is_configured); `run_tests=True` = odpal test-call dla kazdego API z testem.
    """
    return [await _evaluate(conn, run_test=run_tests) for conn in CONNECTIONS]


async def test_connection(key: str) -> ConnectionResult | None:
    """Pojedynczy test na zadanie. None = nieznany klucz."""
    conn = _BY_KEY.get(key)
    if conn is None:
        return None
    return await _evaluate(conn, run_test=True)


async def get_connection_status(key: str) -> ConnectionResult | None:
    """Status pojedynczej integracji BEZ test-calla (po set/clear sekretu)."""
    conn = _BY_KEY.get(key)
    if conn is None:
        return None
    return await _evaluate(conn, run_test=False)


# ── Zapis / odczyt sekretu (tylko 4 edytowalne integracje) ──────────────────


class ConnectionNotEditable(Exception):
    """Connection_key nieznany albo nieedytowalny (stripe/circle). Caller -> 404."""


def _store_key(connection_key: str) -> str:
    store_key = _SECRET_KEY_BY_CONNECTION.get(connection_key)
    if store_key is None:
        raise ConnectionNotEditable(connection_key)
    return store_key


async def set_secret(connection_key: str, value: str, user_id: int | None) -> None:
    """Zapisuje sekret edytowalnej integracji. Mapuje connection_key -> store_key.
    Rzuca ConnectionNotEditable (caller 404) gdy nieedytowalny/nieznany.
    Rzuca secret_box.SecretBoxUnavailable (caller 400) gdy brak master key.
    NIGDY nie loguje wartosci."""
    await secrets.set_secret(_store_key(connection_key), value, user_id)


async def clear_secret(connection_key: str, user_id: int | None) -> None:
    """Usuwa sekret edytowalnej integracji (powrot na env). 404 gdy nieedytowalny."""
    await secrets.clear_secret(_store_key(connection_key), user_id)


async def reveal_secret(connection_key: str, *, env_fallback: bool = True) -> str | None:
    """Pelna efektywna wartosc sekretu (swiadomy reveal za auth). 404 gdy nieedytowalny.
    None gdy ani DB, ani env (brak skonfigurowanej wartosci)."""
    return await secrets.effective_value(_store_key(connection_key), env_fallback=env_fallback)
