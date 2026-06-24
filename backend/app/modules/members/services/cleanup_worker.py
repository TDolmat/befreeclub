"""Worker cleanupu czlonkostw Circle (zamiast publicznego crona circle-cleanup).

Logika biznesowa jest w services/cleanup.run_cleanup ([members]) - ten plik
to TYLKO warstwa workera wg wzorca fazy 1: asyncio task w lifespan, tick +
sleep co interwal cleanupu, guard reentrancy (tick pomijany, nie kolejkowany),
stop = cancel. Reczny trigger admina
(POST /api/billing/admin/workers/membership_cleanup/run) idzie przez run_now()
- serializuje sie z tickiem i zwraca wynik przebiegu.

BRAMKA BEZPIECZENSTWA (admin.settings key `members.cleanup`):
  - tick automatyczny czyta enabled/dryRun PRZED przebiegiem. enabled=false ->
    nic (log debug). enabled=true + dryRun=true -> tryb cienia (loguje kogo by
    usunieto, NIE rusza Circle ani statusu). Tylko enabled=true + dryRun=false
    realnie usuwa.
  - reczny trigger admina to swiadoma akcja czlowieka: NIE jest blokowany
    bramka `enabled`, ale dryRun z ustawien GO OBOWIAZUJE (chyba ze caller
    wprost wymusi tryb przez override) - jak w cleanup-controls.

Swiezy deploy bez wpisu w admin.settings = bezpieczny default
(enabled=false, dryRun=true) z SAFE_DEFAULTS, wiec nikogo nie usuwa.

Harmonogram oryginalu NIEZNANY: cron.job zyje tylko w prod DB Supabase.
Default 6 h (MEMBERSHIP_CLEANUP_INTERVAL_MS) - nadpisywalny w panelu kluczem
admin.settings `members.cleanup_interval_ms` (wymaga restartu workera).
"""

import asyncio
from datetime import UTC, datetime

from app.core.config import settings
from app.core.logging import create_logger, to_iso_string
from app.modules.admin.services.settings import get_effective, get_setting, set_setting
from app.modules.members.services.cleanup import CleanupConfigError, CleanupResult, run_cleanup

log = create_logger("workers.cleanup")

SETTINGS_KEY = "members.cleanup"
LAST_RUN_KEY = "members.cleanup.last_run"
INTERVAL_KEY = "members.cleanup_interval_ms"

_task: asyncio.Task[None] | None = None
_lock = asyncio.Lock()


async def _interval_seconds() -> float:
    """Interwal pollingu workera: admin.settings nadpisuje env (czytane raz
    przy starcie petli, zmiana wymaga restartu workera)."""
    ms = await get_effective(
        INTERVAL_KEY,
        env_fallback=settings.MEMBERSHIP_CLEANUP_INTERVAL_MS,
        safe_default=settings.MEMBERSHIP_CLEANUP_INTERVAL_MS,
    )
    return int(ms) / 1000


async def _record_last_run(result: CleanupResult) -> None:
    """Zapis metadanych ostatniego przebiegu do panelu (admin.settings).
    Bez user_id (akcja systemowa)."""
    await set_setting(
        LAST_RUN_KEY,
        {
            "value": {
                "timestamp": to_iso_string(datetime.now(UTC)),
                "checked": result.checked,
                "wouldRemove": result.would_remove,
                "removed": result.removed,
                "mode": "dry_run" if result.dry_run else "live",
            }
        },
        None,
    )


async def run_now(*, dry_run: bool | None = None) -> CleanupResult:
    """Pojedynczy przebieg cleanupu (trigger admina / tick workera).

    dry_run=None (domyslnie): tryb brany z admin.settings `members.cleanup`
    (dryRun). Reczny trigger admina nie patrzy na `enabled` (swiadoma akcja),
    ale dryRun ustawien obowiazuje. Zapisuje metadane przebiegu do panelu.

    Rzuca CleanupConfigError przy niekompletnej konfiguracji (guard
    z review 2.1) - trigger admina dostaje glosny blad."""
    async with _lock:
        if dry_run is None:
            cfg = await get_setting(SETTINGS_KEY)
            dry_run = bool(cfg.get("dryRun", True))
        mode = "dry-run" if dry_run else "LIVE"
        log.info(f"Cleanup run started (mode={mode})")
        result = await run_cleanup(dry_run=dry_run)
        log.info(
            f"Cleanup run finished (mode={mode}): checked={result.checked} "
            f"wouldRemove={result.would_remove} removed={result.removed}"
        )
        await _record_last_run(result)
        return result


async def _tick() -> None:
    if _lock.locked():
        log.debug("previous run still in progress, skipping tick")
        return
    # BRAMKA: tick automatyczny rusza tylko gdy enabled=true. Brak wpisu /
    # enabled=false = bezpieczny default (nic sie nie dzieje).
    cfg = await get_setting(SETTINGS_KEY)
    if not cfg.get("enabled", False):
        log.debug("cleanup disabled in admin.settings, skipping tick")
        return
    dry_run = bool(cfg.get("dryRun", True))
    try:
        await run_now(dry_run=dry_run)
    except CleanupConfigError as err:
        # Niekompletna konfiguracja (dev bez kluczy / zly deploy): tick
        # pominiety zamiast destrukcyjnego przebiegu; warn, zeby prod
        # bez ktoregos klucza byl widoczny w logach.
        log.warn(f"cleanup not configured, skipping tick: {err}")


async def _loop() -> None:
    interval = await _interval_seconds()
    while True:
        try:
            await _tick()
        except Exception as err:
            # Blad przebiegu nie moze zabic workera.
            log.error(f"tick failed: {err}")
        await asyncio.sleep(interval)


def start_cleanup_worker() -> None:
    global _task
    if _task:
        return
    log.info("Starting membership_cleanup worker")
    _task = asyncio.create_task(_loop())


def stop_cleanup_worker() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None
