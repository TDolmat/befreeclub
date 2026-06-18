"""Port tools/circle-dm/services/polling-worker.ts (1:1 wg docs/spec/services-sync.md
sekcja 1). Bez auto-generacji draftow (wylaczona 2026-05-13, w kodzie TS jej nie ma).

Guard reentrancy: tick pomijany, nie kolejkowany. Pierwszy tick natychmiast przy
starcie, potem co POLLING_INTERVAL_MS (default 30 s, min 5 s - walidacja w config).
"""

import asyncio

from sqlalchemy import select

from app.core.config import settings
from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.core.ws import broker
from app.modules.circle_dm.models import Account
from app.modules.circle_dm.services.thread_sync import (
    sync_messages_for_thread,
    sync_threads_for_account,
)

log = create_logger("polling")

_task: asyncio.Task[None] | None = None
_running = False
_message_syncs: set[asyncio.Task[None]] = set()


async def _tick() -> None:
    global _running
    if _running:
        log.debug("previous tick still running, skipping")
        return
    _running = True
    try:
        async with async_session_maker() as session:
            account_ids = (
                (await session.execute(select(Account.id).where(Account.is_active.is_(True))))
                .scalars()
                .all()
            )

        for account_id in account_ids:
            try:
                result = await sync_threads_for_account(account_id)
                changed_thread_ids = result["changed_thread_ids"]
                if len(changed_thread_ids) > 0:
                    broker.broadcast(
                        {
                            "type": "threads:updated",
                            "adminAccountId": account_id,
                            "changedThreadIds": changed_thread_ids,
                        }
                    )
                for thread_id in result["new_unread_thread_ids"]:
                    broker.broadcast(
                        {"type": "thread:new_messages", "threadId": thread_id, "newCount": 1}
                    )
                # Fire-and-forget dociaganie historii stale threads: odpowiedzi
                # czlonkow, sendy spoza apki, reconciliacja placeholderow oraz
                # watki "stuck" z wczesniejszych tickow.
                for thread_id in result["stale_message_thread_ids"]:
                    _spawn_message_sync(thread_id)
            except Exception as err:
                log.error(f"sync failed for account {account_id}", str(err))
    finally:
        _running = False


def _spawn_message_sync(thread_id: int) -> None:
    task = asyncio.create_task(_sync_messages_safe(thread_id))
    _message_syncs.add(task)
    task.add_done_callback(_message_syncs.discard)


async def _sync_messages_safe(thread_id: int) -> None:
    try:
        await sync_messages_for_thread(thread_id)
    except Exception as err:
        log.warn(f"message sync failed for thread {thread_id}: {err}")


async def _loop() -> None:
    while True:
        try:
            await _tick()
        except Exception as err:
            # Blad spoza petli per konto (np. select kont) nie moze zabic workera.
            log.error(f"tick failed: {err}")
        await asyncio.sleep(settings.POLLING_INTERVAL_MS / 1000)


def start_polling() -> None:
    global _task
    if _task:
        return
    log.info(f"Starting polling worker (interval {settings.POLLING_INTERVAL_MS}ms)")
    _task = asyncio.create_task(_loop())


def stop_polling() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None


async def sync_now(admin_account_id: int | None = None) -> None:
    """Trigger manualny. Z argumentem: samo syncThreadsForAccount (bez WS eventow
    i bez stale-messages follow-upu); bez argumentu: pelny tick."""
    if admin_account_id is not None:
        await sync_threads_for_account(admin_account_id)
    else:
        await _tick()
