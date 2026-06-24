"""Store edytowalnych sekretow integracji (tabela admin.encrypted_secrets, migracja 0004).

4 klucze API edytowalne w panelu admina, zaszyfrowane Fernetem. Env jest
OPCJONALNYM fallbackiem - brak wiersza w bazie = serwis schodzi na env (zachowanie
1:1 ze starym kodem). Ustawienie w panelu NADPISUJE env.

Wzorzec 1:1 z app/modules/admin/services/settings.py:
- cache ~30 s per proces (jeden uvicorn), invalidacja przy kazdym zapisie,
- warstwa DB w funkcjach mozliwych do monkeypatch w testach (_db_load / _db_upsert /
  _db_delete), zeby suite szedl bez Postgresa.

BEZPIECZENSTWO:
- W bazie i logach NIGDY nie ma wartosci jawnej - tylko ciphertext. Logujemy
  wylacznie key (+ akcja); email/user zostawiamy warstwie endpointu.
- Pelna wartosc opuszcza store WYLACZNIE przez effective_value() (reveal) albo
  resolve()/get_secret() (klucz do dostawcy). GET status uzywa mask().
- Brak/zly master key: set_secret() rzuca (caller -> 400), get_secret() zwraca None,
  resolve() schodzi na env. Nigdy crash.
"""

import time
from collections.abc import Awaitable, Callable

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core import secret_box
from app.core.config import settings
from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.modules.admin.models import EncryptedSecret

log = create_logger("admin.secrets")

CACHE_MS = 30_000

# Edytowalne sekrety: store_key -> nazwa pola env (fallback) w app.core.config.settings.
# TYLKO te 4. Stripe/Circle zostaja status-only z env i NIE sa tutaj.
SECRET_KEYS: dict[str, str] = {
    "openai.api_key": "OPENAI_API_KEY",
    "resend.api_key": "RESEND_API_KEY",
    "sender.api_token": "SENDER_API_TOKEN",
    "meta.capi_token": "META_CAPI_TOKEN",
}

# Cache odszyfrowanych wartosci per klucz: store_key -> (plaintext|None, cached_at_ms).
# None = wiersz istnieje, ale nie dalo sie odszyfrowac (zly/brak master key) ALBO
# wiersza brak. Rozroznienie "panel vs brak" robi secret_status() przez _db_load.
_cache: dict[str, tuple[str | None, float]] = {}


# ── Warstwa DB (monkeypatch w testach) ───────────────────────────────────────


async def _db_load(key: str) -> str | None:
    """Zwraca ciphertext z bazy albo None gdy wiersza brak. Monkeypatch w testach."""
    async with async_session_maker() as session:
        return (
            await session.execute(
                select(EncryptedSecret.ciphertext).where(EncryptedSecret.key == key).limit(1)
            )
        ).scalar_one_or_none()


async def _db_upsert(key: str, ciphertext: str, user_id: int | None) -> None:
    """Wstawia/aktualizuje ciphertext. Monkeypatch w testach."""
    stmt = pg_insert(EncryptedSecret).values(
        key=key, ciphertext=ciphertext, updated_by_user_id=user_id
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[EncryptedSecret.key],
        set_={"ciphertext": ciphertext, "updated_by_user_id": user_id},
    )
    async with async_session_maker() as session:
        await session.execute(stmt)
        await session.commit()


async def _db_delete(key: str) -> None:
    """Usuwa wiersz sekretu (brak = fallback na env). Monkeypatch w testach."""
    async with async_session_maker() as session:
        await session.execute(delete(EncryptedSecret).where(EncryptedSecret.key == key))
        await session.commit()


# Typowane aliasy, gdyby ktos chcial podmienic punktowo.
DbLoad = Callable[[str], Awaitable[str | None]]


# ── Helpery ──────────────────────────────────────────────────────────────────


def _env_value(key: str) -> str | None:
    """Wartosc fallbacku z env dla danego store_key (albo None gdy nieznany/pusty)."""
    env_attr = SECRET_KEYS.get(key)
    if env_attr is None:
        return None
    return getattr(settings, env_attr, None)


def _cache_get(key: str) -> str | None | object:
    """Zwraca wartosc z cache (str|None) albo sentinel _MISS gdy cache zimny/wygasl."""
    cached = _cache.get(key)
    if cached and time.time() * 1000 - cached[1] < CACHE_MS:
        return cached[0]
    return _MISS


_MISS = object()


def mask(v: str | None) -> str | None:
    """Maskuje wartosc do podgladu: pierwsze 4 + '…' + ostatnie 4.
    Krotka wartosc (<12 znakow) -> '••••' (zeby nie odslonic wiekszosci krotkiego
    tokenu). None -> None. Realne klucze (sk-/re_/Bearer) sa dlugie, to defensywa."""
    if v is None:
        return None
    if len(v) < 12:
        return "••••"
    return f"{v[:4]}…{v[-4:]}"


def invalidate_cache() -> None:
    """Czysci caly cache sekretow (np. testy albo po zapisie)."""
    _cache.clear()


# ── Zapis / odczyt ───────────────────────────────────────────────────────────


async def set_secret(key: str, plaintext: str, user_id: int | None) -> None:
    """Szyfruje i zapisuje sekret pod store_key. Aktualizuje cache.

    Rzuca secret_box.SecretBoxUnavailable gdy master key niedostepny (caller -> 400).
    Rzuca KeyError gdy key spoza SECRET_KEYS (caller -> 404).
    user_id (admin.users.id) -> updated_by_user_id; 0 z dev bypassu = NULL."""
    if key not in SECRET_KEYS:
        raise KeyError(key)
    ciphertext = secret_box.encrypt(plaintext)  # rzuca SecretBoxUnavailable gdy brak klucza
    by = user_id if user_id else None
    await _db_upsert(key, ciphertext, by)
    _cache[key] = (plaintext, time.time() * 1000)
    log.info("secret set", {"key": key})


async def get_secret(key: str) -> str | None:
    """Odszyfrowana wartosc sekretu z bazy (bez env). Cache ~30 s.
    None gdy: key nieznany, brak wiersza, brak/zly master key, uszkodzony token."""
    if key not in SECRET_KEYS:
        return None
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached  # type: ignore[return-value]
    try:
        ciphertext = await _db_load(key)
    except Exception:  # noqa: BLE001 - blad DB nie moze wywrocic odczytu sekretu
        log.warn("secret db load failed", {"key": key})
        return None
    if ciphertext is None:
        # Nie cache'ujemy braku wiersza pod realnym kluczem - zapis ma dotrzec od razu.
        return None
    plaintext = secret_box.decrypt(ciphertext)
    _cache[key] = (plaintext, time.time() * 1000)
    return plaintext


def get_secret_sync(key: str) -> str | None:
    """Synchroniczny odczyt sekretu - TYLKO z cache, NIGDY nie dotyka DB.

    Last-known-good: zwraca ostatnia znana wartosc z cache nawet po wygasnieciu TTL.
    Sync konsument (worker, klient) nie moze odswiezyc z DB, wiec lepiej oddac
    ostatnia dobra wartosc niz None i oscylowac co 30 s (worker by stawal). Cache
    odswieza async get_secret (po TTL) oraz set_secret/clear_secret. warm_cache()
    laduje wszystkie 4 na starcie, zeby po restarcie klucz z panelu dzialal od razu.
    None tylko gdy cache nigdy nie ogrzany pod tym kluczem -> caller schodzi na env."""
    cached = _cache.get(key)
    if cached is not None:
        return cached[0]
    return None


async def clear_secret(key: str, user_id: int | None) -> None:
    """Usuwa sekret z bazy (powrot na env fallback). Czysci cache.
    Rzuca KeyError gdy key spoza SECRET_KEYS (caller -> 404). user_id do audytu/logu."""
    if key not in SECRET_KEYS:
        raise KeyError(key)
    await _db_delete(key)
    _cache.pop(key, None)
    log.info("secret cleared", {"key": key, "by": user_id or None})


# ── Resolve (DB > env > None) ────────────────────────────────────────────────


async def resolve(key: str, *, env_fallback: bool = True) -> str | None:
    """Efektywna wartosc do uzycia jako klucz dostawcy: DB (decrypt) > env > None.
    env_fallback=False wylacza krok env (None gdy brak wiersza/decryptu)."""
    db_value = await get_secret(key)
    if db_value is not None:
        return db_value
    if env_fallback:
        return _env_value(key)
    return None


def resolve_sync(key: str, *, env_fallback: bool = True) -> str | None:
    """Synchroniczny resolve: cache DB > env > None. NIGDY nie dotyka DB.
    Cache zimny -> env_fallback (zachowanie 1:1, async resolve ogrzeje cache)."""
    db_value = get_secret_sync(key)
    if db_value is not None:
        return db_value
    if env_fallback:
        return _env_value(key)
    return None


# ── Status / reveal (do panelu) ──────────────────────────────────────────────


async def secret_status(key: str, *, env_fallback: bool = True) -> str:
    """Zrodlo wartosci BEZ ujawniania jej:
    - 'panel' gdy istnieje wiersz w bazie (niezaleznie od stanu master key),
    - 'env'   gdy wiersza brak, ale env_fallback ma wartosc,
    - 'brak'  gdy ani DB, ani env.
    Czyta sam fakt obecnosci wiersza (_db_load), NIE odszyfrowuje wartosci."""
    if key not in SECRET_KEYS:
        return "brak"
    try:
        ciphertext = await _db_load(key)
    except Exception:  # noqa: BLE001 - blad DB nie moze wywrocic listingu Polaczen
        log.warn("secret status db load failed", {"key": key})
        ciphertext = None
    if ciphertext is not None:
        return "panel"
    if env_fallback and _env_value(key):
        return "env"
    return "brak"


async def warm_cache() -> None:
    """Laduje 4 sekrety z DB do cache na starcie (wolane w lifespan przed workerami).

    Bez tego po RESTARCIE procesu sync konsumenci (workery, klienci) widza zimny
    cache i schodza na env. W trybie panel-only-bez-env oznaczaloby to brak klucza
    do czasu pierwszego otwarcia Polaczen - workery by stawaly. Ogrzanie zamyka to
    okno. Blad DB/decrypt nie przerywa startu (get_secret juz to lapie)."""
    for key in SECRET_KEYS:
        try:
            await get_secret(key)
        except Exception:  # noqa: BLE001 - start nie moze pasc przez sekrety
            log.warn("secret warm failed", {"key": key})


async def effective_value(key: str, *, env_fallback: bool = True) -> str | None:
    """Pelna wartosc efektywna do REVEAL: DB (decrypt) > env > None.
    Identyczna precedencja co resolve() - reveal pokazuje to, czego realnie uzyje
    dostawca. Caller chroni to za auth; GET status uzywa mask(effective_value(...))."""
    return await resolve(key, env_fallback=env_fallback)
