"""Worker ponawiania nieudanych zaproszen Circle (port retry-circle-invites).

Logika biznesowa jest w services/maintenance.retry_failed_invites ([members]) -
z NAPRAWA #6 z PLAN_LANDING: ponawia TYLKO status invite_failed, nigdy
removed (oryginal re-invitowal celowo wyrzuconych). Ten plik to warstwa
workera wg wzorca fazy 1: asyncio task w lifespan, tick + sleep co
INVITE_RETRY_INTERVAL_MS (default 1 h), guard reentrancy, stop = cancel.
Reczny trigger admina (POST /api/billing/admin/workers/invite_retry/run)
idzie przez run_now() - serializuje sie z tickiem i zwraca wynik przebiegu.
"""

import asyncio

from app.core.config import settings
from app.core.logging import create_logger
from app.modules.members.services.maintenance import RetryResult, retry_failed_invites

log = create_logger("workers.retry")

_task: asyncio.Task[None] | None = None
_lock = asyncio.Lock()


async def run_now() -> list[RetryResult]:
    """Pojedynczy przebieg retry (trigger admina / tick workera)."""
    async with _lock:
        log.info("Invite retry run started")
        results = await retry_failed_invites()
        succeeded = sum(1 for r in results if r.success)
        log.info(f"Invite retry run finished: attempted={len(results)} succeeded={succeeded}")
        return results


async def _tick() -> None:
    if _lock.locked():
        log.debug("previous run still in progress, skipping tick")
        return
    await run_now()


async def _loop() -> None:
    while True:
        try:
            await _tick()
        except Exception as err:
            # Blad przebiegu nie moze zabic workera.
            log.error(f"tick failed: {err}")
        await asyncio.sleep(settings.INVITE_RETRY_INTERVAL_MS / 1000)


def start_invite_retry_worker() -> None:
    global _task
    if _task:
        return
    log.info(f"Starting invite_retry worker (interval {settings.INVITE_RETRY_INTERVAL_MS}ms)")
    _task = asyncio.create_task(_loop())


def stop_invite_retry_worker() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None
