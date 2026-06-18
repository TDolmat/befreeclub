"""Port tools/circle-dm/services/send.ts (1:1 wg docs/spec/services-sync.md sekcja 5).

Kolejnosc operacji jest kontraktem: audyt PRZED wysylka; po sukcesie update audytu,
mark_sent (czyszczenie draftu + WS draft:status), auto-done check-upow, syntetyczny
placeholder (-epoch ms), bump watku, fire-and-forget mark_chat_room_read, dwa
AWAITOWANE post-synci z polknietym bledem, na koncu WS send:result i return.
Blad wysylki NIE rzuca (wraca jako {ok:false}); wyjatkiem brak watku, ktory rzuca.
"""

import asyncio
import json
import time
from datetime import UTC, datetime

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.core.ws import broker
from app.modules.circle_dm.circle.client import (
    CircleApiError,
    mark_chat_room_read,
    send_message,
)
from app.modules.circle_dm.circle.jwt_manager import get_jwt_for, invalidate_jwt
from app.modules.circle_dm.models import Account, Message, SentMessage, Thread
from app.modules.circle_dm.services.draft_orchestrator import mark_sent
from app.modules.circle_dm.services.thread_state import clear_pending_checkups_on_send
from app.modules.circle_dm.services.thread_sync import (
    sync_messages_for_thread,
    sync_threads_for_account,
)

log = create_logger("send")

_background: set[asyncio.Task[None]] = set()


def _parse_date(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _mark_read_safe(jwt_token: str, chat_room_uuid: str, thread_id: int) -> None:
    try:
        await mark_chat_room_read(jwt_token, chat_room_uuid)
    except Exception as err:
        log.warn(f"mark-as-read failed for thread {thread_id}: {err}")


async def send_draft(thread_id: int, body: str) -> dict:
    async with async_session_maker() as session:
        thread = (
            await session.execute(
                select(Thread.id, Thread.account_id, Thread.circle_chat_room_uuid)
                .where(Thread.id == thread_id)
                .limit(1)
            )
        ).first()
    if thread is None:
        raise Exception(f"thread {thread_id} not found")

    # Wiersz audytu powstaje zawsze, nawet gdy send sie wywali.
    async with async_session_maker() as session:
        audit_id = (
            await session.execute(
                insert(SentMessage)
                .values(thread_id=thread_id, body=body)
                .returning(SentMessage.id)
            )
        ).scalar_one()
        await session.commit()

    try:
        jwt = await get_jwt_for(thread.account_id)
        result = await send_message(jwt.access_token, thread.circle_chat_room_uuid, body)
        log.info(
            "Circle send response",
            json.dumps(result, ensure_ascii=False, separators=(",", ":"))[:400],
        )

        # Circle zwraca {creation_uuid, parent_message_id, sent_at}; numeryczne id
        # zwykle nie przychodzi.
        rid = result.get("id")
        async with async_session_maker() as session:
            await session.execute(
                update(SentMessage)
                .where(SentMessage.id == audit_id)
                .values(
                    circle_message_id=(
                        rid if isinstance(rid, int) and not isinstance(rid, bool) else None
                    ),
                    circle_creation_uuid=result.get("creation_uuid"),
                )
            )
            await session.commit()

        await mark_sent(thread_id)
        await clear_pending_checkups_on_send(thread_id)

        # Syntetyczny placeholder (eventual consistency Circle) - ujemny epoch ms,
        # sprzatany przez reconciliacje w sync_messages_for_thread.
        async with async_session_maker() as session:
            account = (
                await session.execute(
                    select(Account.label, Account.community_member_id)
                    .where(Account.id == thread.account_id)
                    .limit(1)
                )
            ).first()

        sent_at = _parse_date(result["sent_at"]) if result.get("sent_at") else datetime.now(UTC)

        async with async_session_maker() as session:
            await session.execute(
                pg_insert(Message)
                .values(
                    thread_id=thread_id,
                    circle_message_id=-int(time.time() * 1000),
                    body=body,
                    rich_text_body=None,
                    sender_id=account.community_member_id if account else None,
                    sender_name=account.label if account else None,
                    sender_is_me=True,
                    parent_message_id=None,
                    chat_thread_id=None,
                    created_at=sent_at,
                    edited_at=None,
                )
                .on_conflict_do_nothing()
            )
            await session.commit()

            await session.execute(
                update(Thread)
                .where(Thread.id == thread_id)
                .values(
                    last_message_at=sent_at,
                    last_message_preview=body[:240],
                    last_message_sender_id=account.community_member_id if account else None,
                    last_message_sender_is_me=True,
                    unread_messages_count=0,
                )
            )
            await session.commit()

        task = asyncio.create_task(
            _mark_read_safe(jwt.access_token, thread.circle_chat_room_uuid, thread_id)
        )
        _background.add(task)
        task.add_done_callback(_background.discard)

        # Awaitowane celowo - frontend liczy na swieze dane zaraz po sendzie.
        try:
            await sync_messages_for_thread(thread_id)
        except Exception as err:
            log.warn(f"post-send message sync failed: {err}")
        try:
            await sync_threads_for_account(thread.account_id)
        except Exception as err:
            log.warn(f"post-send thread sync failed: {err}")

        audit_message_id = result.get("id")

        broker.broadcast(
            {
                "type": "send:result",
                "threadId": thread_id,
                "ok": True,
                "circleMessageId": audit_message_id,
            }
        )

        creation_uuid = result.get("creation_uuid")
        log.info(
            f"sent message to thread {thread_id} "
            f"(creation_uuid={creation_uuid if creation_uuid is not None else 'n/a'})"
        )
        return {"ok": True, "circleMessageId": audit_message_id}
    except Exception as err:
        message = str(err)
        log.error(f"send failed for thread {thread_id}", message)

        if isinstance(err, CircleApiError) and err.status == 401:
            await invalidate_jwt(thread.account_id)

        async with async_session_maker() as session:
            await session.execute(
                update(SentMessage).where(SentMessage.id == audit_id).values(error=message)
            )
            await session.commit()

        broker.broadcast(
            {
                "type": "send:result",
                "threadId": thread_id,
                "ok": False,
                "circleMessageId": None,
                "error": message,
            }
        )

        return {"ok": False, "circleMessageId": None, "error": message}
