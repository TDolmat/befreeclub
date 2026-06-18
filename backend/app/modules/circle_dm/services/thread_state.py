"""Port tools/circle-dm/services/thread-state.ts (1:1 wg docs/spec/services-sync.md
sekcja 4): statusy inbox/done, flagi, bulk akcje scoped do konta, check-upy,
auto-revival done -> inbox.

Swiadome naprawy quirkow oryginalu (docs/spec/port-odstepstwa.md):
- set_thread_status/set_thread_flagged zwracaja bool (False = brak watku,
  route mapuje na 404 zamiast falszywego {ok:true}),
- mark_checkup_done/delete_checkup scoped do thread_id (oryginal ignorowal
  :id watku z URL, dalo sie mutowac checkup cudzego watku)."""

from datetime import UTC, datetime

from sqlalchemy import delete, insert, select, update

from app.core.db import async_session_maker
from app.core.logging import create_logger, to_iso_string
from app.modules.circle_dm.models import Checkup, Thread

log = create_logger("thread-state")


async def set_thread_status(thread_id: int, status: str) -> bool:
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                update(Thread)
                .where(Thread.id == thread_id)
                .values(status=status)
                .returning(Thread.id)
            )
        ).all()
        await session.commit()
    log.debug(f"thread {thread_id} → {status}")
    return len(rows) > 0


async def set_thread_flagged(thread_id: int, is_flagged: bool) -> bool:
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                update(Thread)
                .where(Thread.id == thread_id)
                .values(is_flagged=is_flagged)
                .returning(Thread.id)
            )
        ).all()
        await session.commit()
    return len(rows) > 0


async def bulk_set_status(account_id: int, thread_ids: list[int], status: str) -> int:
    if not thread_ids:
        return 0
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                update(Thread)
                .where(Thread.account_id == account_id, Thread.id.in_(thread_ids))
                .values(status=status)
                .returning(Thread.id)
            )
        ).all()
        await session.commit()
    log.info(f"bulk status → {status} for {len(rows)} thread(s), account {account_id}")
    return len(rows)


async def bulk_set_flagged(account_id: int, thread_ids: list[int], is_flagged: bool) -> int:
    if not thread_ids:
        return 0
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                update(Thread)
                .where(Thread.account_id == account_id, Thread.id.in_(thread_ids))
                .values(is_flagged=is_flagged)
                .returning(Thread.id)
            )
        ).all()
        await session.commit()
    flag = "true" if is_flagged else "false"
    log.info(f"bulk flag → {flag} for {len(rows)} thread(s), account {account_id}")
    return len(rows)


def _serialize(row) -> dict:  # noqa: ANN001 - przyjmuje ORM Checkup i Row z RETURNING
    return {
        "id": row.id,
        "threadId": row.thread_id,
        "dueAt": to_iso_string(row.due_at),
        "note": row.note,
        "doneAt": to_iso_string(row.done_at) if row.done_at is not None else None,
        "createdAt": to_iso_string(row.created_at),
    }


async def list_checkups(thread_id: int) -> list[dict]:
    async with async_session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(Checkup)
                    .where(Checkup.thread_id == thread_id)
                    .order_by(Checkup.due_at.asc())
                )
            )
            .scalars()
            .all()
        )
    return [_serialize(r) for r in rows]


async def create_checkup(thread_id: int, due_at: datetime, note: str | None) -> dict:
    async with async_session_maker() as session:
        row = (
            await session.execute(
                insert(Checkup)
                .values(thread_id=thread_id, due_at=due_at, note=note)
                .returning(
                    Checkup.id,
                    Checkup.thread_id,
                    Checkup.due_at,
                    Checkup.note,
                    Checkup.done_at,
                    Checkup.created_at,
                )
            )
        ).one()
        await session.commit()
    return _serialize(row)


async def mark_checkup_done(thread_id: int, checkup_id: int) -> None:
    async with async_session_maker() as session:
        await session.execute(
            update(Checkup)
            .where(Checkup.id == checkup_id, Checkup.thread_id == thread_id)
            .values(done_at=datetime.now(UTC))
        )
        await session.commit()


async def delete_checkup(thread_id: int, checkup_id: int) -> None:
    async with async_session_maker() as session:
        await session.execute(
            delete(Checkup).where(Checkup.id == checkup_id, Checkup.thread_id == thread_id)
        )
        await session.commit()


async def clear_pending_checkups_on_send(thread_id: int) -> None:
    """Po sendzie wszystkie wiszace check-upy uznajemy za zalatwione."""
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                update(Checkup)
                .where(Checkup.thread_id == thread_id, Checkup.done_at.is_(None))
                .values(done_at=datetime.now(UTC))
                .returning(Checkup.id)
            )
        ).all()
        await session.commit()
    if rows:
        log.debug(f"auto-cleared {len(rows)} pending check-up(s) for thread {thread_id}")


async def revive_if_done(thread_id: int) -> bool:
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                update(Thread)
                .where(Thread.id == thread_id, Thread.status == "done")
                .values(status="inbox")
                .returning(Thread.id)
            )
        ).all()
        await session.commit()
    if rows:
        log.info(f"thread {thread_id} auto-revived from done → inbox (new incoming)")
        return True
    return False


async def get_latest_pending_checkup(thread_id: int) -> dict | None:
    async with async_session_maker() as session:
        row = (
            await session.execute(
                select(Checkup)
                .where(Checkup.thread_id == thread_id, Checkup.done_at.is_(None))
                .order_by(Checkup.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return _serialize(row) if row is not None else None
