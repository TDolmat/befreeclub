"""Worker cleanupu czlonkostw Circle (zamiast publicznego crona circle-cleanup).

Logika biznesowa jest w services/cleanup.run_cleanup ([members]) - ten plik
to TYLKO warstwa workera wg wzorca fazy 1: asyncio task w lifespan, tick +
sleep co MEMBERSHIP_CLEANUP_INTERVAL_MS, guard reentrancy (tick pomijany,
nie kolejkowany), stop = cancel. Reczny trigger admina
(POST /api/billing/admin/workers/membership_cleanup/run) idzie przez
run_now() - serializuje sie z tickiem i zwraca wynik przebiegu.

Harmonogram oryginalu NIEZNANY: cron.job zyje tylko w prod DB Supabase.
Default 6 h - do potwierdzenia po zrzucie `SELECT * FROM cron.job`
(PLAN_LANDING, sekcja migracji, punkt 1).
"""

import asyncio

from app.core.config import settings
from app.core.logging import create_logger
from app.modules.members.services.cleanup import CleanupConfigError, CleanupResult, run_cleanup

log = create_logger("workers.cleanup")

_task: asyncio.Task[None] | None = None
_lock = asyncio.Lock()


async def run_now() -> CleanupResult:
    """Pojedynczy przebieg cleanupu (trigger admina / tick workera).
    Rzuca CleanupConfigError przy niekompletnej konfiguracji (guard
    z review 2.1) - trigger admina dostaje glosny blad."""
    async with _lock:
        log.info("Cleanup run started")
        result = await run_cleanup()
        log.info(f"Cleanup run finished: checked={result.checked} removed={result.removed}")
        return result


async def _tick() -> None:
    if _lock.locked():
        log.debug("previous run still in progress, skipping tick")
        return
    try:
        await run_now()
    except CleanupConfigError as err:
        # Niekompletna konfiguracja (dev bez kluczy / zly deploy): tick
        # pominiety zamiast destrukcyjnego przebiegu; warn, zeby prod
        # bez ktoregos klucza byl widoczny w logach.
        log.warn(f"cleanup not configured, skipping tick: {err}")


async def _loop() -> None:
    while True:
        try:
            await _tick()
        except Exception as err:
            # Blad przebiegu nie moze zabic workera.
            log.error(f"tick failed: {err}")
        await asyncio.sleep(settings.MEMBERSHIP_CLEANUP_INTERVAL_MS / 1000)


def start_cleanup_worker() -> None:
    global _task
    if _task:
        return
    log.info(
        f"Starting membership_cleanup worker "
        f"(interval {settings.MEMBERSHIP_CLEANUP_INTERVAL_MS}ms)"
    )
    _task = asyncio.create_task(_loop())


def stop_cleanup_worker() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None
