"""Centralny serwis ustawien panelu admina (tabela admin.settings, migracja 0003).

Generyczny key/value store: get_setting(key) / set_setting(key, value, user_id).
Cache ~30 s per proces (wzorzec circle_dm/services/app_settings.py), invalidacja
przy KAZDYM zapisie. Pierwszy konsument: bramki destrukcyjnych workerow czlonkostw
(cleanup / klarna_reconcile / invite_retry).

ZELAZNA ZASADA BEZPIECZENSTWA: brak konfiguracji = bezpiecznie WYLACZONE.
Gdy wiersza brak w bazie (albo wartosc nie ma klucza), helpery zwracaja
bezpieczny default - NIGDY enabled=true. Dzieki temu swiezy deploy bez
zaseedowanych/recznie wlaczonych ustawien nie odpala zadnej destrukcyjnej akcji.
Kontrakt i semantyka kluczy: docs/spec-landing/cleanup-controls.md.
"""

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.modules.admin.models import Setting

log = create_logger("admin.settings")

CACHE_MS = 30_000

# Bezpieczne domysly per klucz. Brak wiersza w bazie -> te wartosci.
# enabled=false wszedzie; dryRun=true tam, gdzie akcja jest destrukcyjna
# (cleanup usuwa czlonkow z Circle). NIGDY nie zakladaj enabled=true.
SAFE_DEFAULTS: dict[str, dict[str, Any]] = {
    "members.cleanup": {"enabled": False, "dryRun": True},
    "members.klarna_reconcile": {"enabled": False},
    "members.invite_retry": {"enabled": False},
}

# Fallback gdy klucz nie ma jawnego wpisu w SAFE_DEFAULTS - zawsze wylaczone.
_GENERIC_SAFE_DEFAULT: dict[str, Any] = {"enabled": False, "dryRun": True}

# Cache per klucz: key -> (value, cached_at_ms).
_cache: dict[str, tuple[dict[str, Any], float]] = {}


def safe_default_for(key: str) -> dict[str, Any]:
    """Bezpieczny default dla klucza (kopia, zeby caller nie zmodyfikowal wzorca)."""
    return dict(SAFE_DEFAULTS.get(key, _GENERIC_SAFE_DEFAULT))


async def _load_fresh(key: str) -> dict[str, Any]:
    try:
        async with async_session_maker() as session:
            row = (
                await session.execute(select(Setting.value).where(Setting.key == key).limit(1))
            ).scalar_one_or_none()
    except Exception as err:  # noqa: BLE001
        # Baza ustawien chwilowo niedostepna (brak tabeli/DB/sieci) - traktujemy
        # jak brak wiersza: bezpieczny default, czyli get_effective zejdzie na
        # env fallback (zachowanie 1:1). Knob nie moze wywracac zadania. Lapiemy
        # szeroko, bo surowy blad sterownika (asyncpg) z fazy laczenia nie zawsze
        # jest opakowany w SQLAlchemyError. Odczyt ma bezpieczny fallback, wiec
        # zlapanie szerokie nie maskuje bledu logiki - tylko chroni request.
        log.warn(f"admin.settings read failed for {key!r}, using safe default: {err}")
        return safe_default_for(key)
    if row is None:
        # Brak wiersza = bezpiecznie wylaczone. NIE cache'ujemy domyslu pod
        # realnym kluczem, zeby seed/zapis dotarl od razu po pojawieniu sie wiersza.
        return safe_default_for(key)
    # Domysl jako baza, nadpisany tym, co realnie jest w bazie - czesciowy
    # wiersz (np. {"enabled": true} bez dryRun) wciaz ma bezpieczne brakujace pola.
    value = {**safe_default_for(key), **row}
    _cache[key] = (value, time.time() * 1000)
    return value


async def get_setting(key: str) -> dict[str, Any]:
    """Zwraca wartosc ustawienia (dict). Gdy wiersza brak -> bezpieczny default
    (enabled=false). Cache ~30 s per proces."""
    cached = _cache.get(key)
    if cached and time.time() * 1000 - cached[1] < CACHE_MS:
        return dict(cached[0])
    return dict(await _load_fresh(key))


async def get_effective(
    key: str,
    *,
    env_fallback: Any = None,
    safe_default: Any = None,
) -> Any:
    """Efektywna wartosc pojedynczego knoba wg reguly: DB nadpisuje env,
    brak obu = bezpieczny default.

    Precedencja:
      1. admin.settings[key]["value"] gdy istnieje wiersz Z polem "value"
         (DB nadpisuje wszystko),
      2. env_fallback gdy nie None (wartosc z app.core.config.settings),
      3. safe_default (gdy podany) albo None.

    Kontrakt bezpieczenstwa: dla TOGGLE/worker NIGDY nie podawaj
    safe_default=True przy nieobecnym env. Nic destrukcyjnego nie wlacza
    sie samo - brak wiersza i brak env = wartosc bezpieczna (False/wylaczone).

    Uwaga: ten helper obsluguje SKALARNE knoby trzymane jako {"value": ...}
    (TUNABLE: modele, interwaly, progi, ID). Bramki workerow (cleanup,
    klarna_reconcile, invite_retry) maja wlasny ksztalt {"enabled":...,
    "dryRun":...} i czyta je get_setting(key) z SAFE_DEFAULTS - tam regula
    'brak = wylaczone' jest juz wbudowana, get_effective ich nie dotyczy.
    """
    raw = await get_setting(key)
    if isinstance(raw, dict) and "value" in raw:
        return raw["value"]
    if env_fallback is not None:
        return env_fallback
    return safe_default


def get_effective_sync(key: str, *, env_fallback: Any = None, safe_default: Any = None) -> Any:
    """Synchroniczny odpowiednik get_effective dla call-site'ow, ktore NIE sa
    async (np. sender.group_ids(), circle._credentials()).

    Czyta z TEGO SAMEGO in-memory cache co wersja async - NIGDY nie dotyka DB.
    - cache cieply i wiersz ma pole "value" -> wartosc z DB (panel nadpisal env),
    - cache cieply, ale wiersz bez "value" (brak nadpisania) -> env_fallback,
    - cache ZIMNY (jeszcze nieogrzany przez sciezke async) -> env_fallback;
      pierwszy odczyt jest "dotychczasowy 1:1", a kolejne wywolanie async
      get_effective/get_setting ogrzeje cache i nastepne odczyty zlapia DB
      (do ~30 s opoznienia, bez restartu, bez blokowania sync call-site'a).

    Precedencja jak w get_effective: DB > env_fallback > safe_default.
    """
    cached = _cache.get(key)
    if cached and time.time() * 1000 - cached[1] < CACHE_MS:
        value = cached[0]
        if isinstance(value, dict) and "value" in value:
            return value["value"]
    if env_fallback is not None:
        return env_fallback
    return safe_default


async def set_setting(key: str, value: dict[str, Any], user_id: int | None) -> dict[str, Any]:
    """Zapisuje wartosc (pelny dict pod kluczem), invaliduje cache, zwraca stan
    po zapisie. user_id (admin.users.id) trafia do updated_by_user_id; 0 z dev
    bypassu auth traktujemy jak brak (NULL), bo nie ma takiego usera w bazie."""
    by = user_id if user_id else None
    stmt = pg_insert(Setting).values(key=key, value=value, updated_by_user_id=by)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Setting.key],
        set_={"value": value, "updated_by_user_id": by},
    )
    async with async_session_maker() as session:
        await session.execute(stmt)
        await session.commit()
    _cache.pop(key, None)
    return await get_setting(key)


def invalidate_cache() -> None:
    """Czysci caly cache ustawien (np. testy)."""
    _cache.clear()
