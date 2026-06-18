"""Port tools/circle-dm/services/thread-sync.ts (1:1 wg docs/spec/services-sync.md
sekcja 2).

Polling pokrywa tylko top 50 watkow (jedna strona listThreads) i 100 ostatnich
wiadomosci watku (jedna strona getThreadMessages) - celowe ograniczenia, bez
paginacji. Syntetyczne placeholdery po sendzie (ujemne circle_message_id) sa
sprzatane po dopasowaniu znormalizowanego body do realnych wiadomosci z Circle.
"""

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.core.ws import broker
from app.modules.circle_dm.circle.attachments import extract_attachments
from app.modules.circle_dm.circle.client import (
    CircleApiError,
    get_thread_messages,
    list_threads,
)
from app.modules.circle_dm.circle.jwt_manager import get_jwt_for, invalidate_jwt
from app.modules.circle_dm.circle.tiptap import tiptap_to_plain_text
from app.modules.circle_dm.models import Account, Message, MessageImageDescription, Thread
from app.modules.circle_dm.services.thread_state import revive_if_done

log = create_logger("thread-sync")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MS = timedelta(milliseconds=1)


def _parse_date(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _epoch_ms(dt: datetime | None) -> int:
    """Odpowiednik (date?.getTime() ?? 0) - porownania na rozdzielczosci ms."""
    if dt is None:
        return 0
    return (dt.astimezone(UTC) - _EPOCH) // _MS


def _pick_plain_text(record: dict) -> str:
    rich = record.get("rich_text_body")
    from_rich = tiptap_to_plain_text(rich) if rich is not None else ""
    if len(from_rich) > 0:
        return from_rich
    body = record.get("body")
    return body if isinstance(body, str) else ""


def _normalize_for_match(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


async def sync_threads_for_account(account_id: int) -> dict:
    jwt = await get_jwt_for(account_id)

    try:
        response = await list_threads(jwt.access_token, per_page=50)
    except CircleApiError as err:
        if err.status == 401:
            await invalidate_jwt(account_id)
        raise

    changed_thread_ids: list[int] = []
    new_unread_thread_ids: list[int] = []
    stale_message_thread_ids: list[int] = []

    for record in response["records"]:
        thread_id, changed, new_unread, stale_messages = await _upsert_thread(
            account_id, jwt.community_member_id, record
        )
        if changed:
            changed_thread_ids.append(thread_id)
        if new_unread:
            new_unread_thread_ids.append(thread_id)
        if stale_messages:
            stale_message_thread_ids.append(thread_id)

    async with async_session_maker() as session:
        await session.execute(
            update(Account).where(Account.id == account_id).values(last_synced_at=datetime.now(UTC))
        )
        await session.commit()

    return {
        "changed_thread_ids": changed_thread_ids,
        "new_unread_thread_ids": new_unread_thread_ids,
        "stale_message_thread_ids": stale_message_thread_ids,
    }


async def _upsert_thread(
    account_id: int, community_member_id: int, record: dict
) -> tuple[int, bool, bool, bool]:
    previews = record["other_participants_preview"]
    other = previews[0] if len(previews) > 0 else None
    last_msg = record.get("last_message")
    last_msg_at = _parse_date(last_msg["created_at"]) if last_msg else None
    sender = (last_msg or {}).get("sender") or {}
    last_sender_is_me = sender.get("community_member_id") == community_member_id

    # Semantyka lokalnego unread: nasza ostatnia wiadomosc = watek przeczytany,
    # niezaleznie od badge'a Circle.
    local_unread = 0 if last_sender_is_me else record["unread_messages_count"]

    last_msg_body = (last_msg or {}).get("body")
    values = {
        "account_id": account_id,
        "circle_chat_room_id": record["id"],
        "circle_chat_room_uuid": record["uuid"],
        "chat_room_kind": record["chat_room_kind"],
        "chat_room_name": record.get("chat_room_name"),
        "other_participant_email": (other or {}).get("email"),
        "other_participant_name": (other or {}).get("name"),
        "other_participant_id": (other or {}).get("community_member_id"),
        "other_participant_avatar_url": (other or {}).get("avatar_url"),
        "unread_messages_count": local_unread,
        "pinned_at": _parse_date(record["pinned_at"]) if record.get("pinned_at") else None,
        "last_message_at": last_msg_at,
        "last_message_sender_id": sender.get("community_member_id"),
        "last_message_sender_is_me": last_sender_is_me,
        "last_message_preview": last_msg_body[:240] if isinstance(last_msg_body, str) else None,
        "raw_payload": record,
        "fetched_at": datetime.now(UTC),
    }

    async with async_session_maker() as session:
        existing = (
            await session.execute(
                select(
                    Thread.id,
                    Thread.unread_messages_count,
                    Thread.last_message_at,
                    Thread.messages_fetched_at,
                )
                .where(
                    Thread.account_id == account_id,
                    Thread.circle_chat_room_uuid == record["uuid"],
                )
                .limit(1)
            )
        ).first()

        if existing is not None:
            changed = existing.unread_messages_count != local_unread or _epoch_ms(
                existing.last_message_at
            ) != _epoch_ms(last_msg_at)
            new_unread = local_unread > existing.unread_messages_count
            stale_messages = last_msg_at is not None and (
                existing.messages_fetched_at is None
                or _epoch_ms(existing.messages_fetched_at) < _epoch_ms(last_msg_at)
            )
            await session.execute(update(Thread).where(Thread.id == existing.id).values(**values))
            await session.commit()
            return existing.id, changed, new_unread, stale_messages

        inserted_id = (
            await session.execute(insert(Thread).values(**values).returning(Thread.id))
        ).scalar_one()
        await session.commit()
        return inserted_id, True, local_unread > 0, last_msg_at is not None


async def sync_messages_for_thread(thread_id: int) -> int:
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

    jwt = await get_jwt_for(thread.account_id)
    try:
        response = await get_thread_messages(
            jwt.access_token, thread.circle_chat_room_uuid, per_page=100
        )
    except CircleApiError as err:
        if err.status == 401:
            await invalidate_jwt(thread.account_id)
        raise

    inserted = 0
    for record in response["records"]:
        if await _upsert_message(thread.id, jwt.community_member_id, record):
            inserted += 1

    # Reconciliacja syntetycznych placeholderow: porownanie body po normalizacji
    # whitespace, zeby drobna roznica ksztaltu z Circle nie zostawila dubla w UI.
    real_bodies_from_me: set[str] = set()
    for record in response["records"]:
        if (record.get("sender") or {}).get("community_member_id") == jwt.community_member_id:
            normalized = _normalize_for_match(_pick_plain_text(record))
            if len(normalized) > 0:
                real_bodies_from_me.add(normalized)
    if real_bodies_from_me:
        async with async_session_maker() as session:
            synthetic_rows = (
                await session.execute(
                    select(Message.id, Message.body).where(
                        Message.thread_id == thread_id,
                        Message.circle_message_id < 0,
                        Message.sender_is_me.is_(True),
                    )
                )
            ).all()
            to_delete = [
                r.id
                for r in synthetic_rows
                if r.body is not None and _normalize_for_match(r.body) in real_bodies_from_me
            ]
            if to_delete:
                await session.execute(delete(Message).where(Message.id.in_(to_delete)))
                await session.commit()
                log.debug(f"Thread {thread_id}: removed {len(to_delete)} synthetic placeholder(s)")

    async with async_session_maker() as session:
        await session.execute(
            update(Thread).where(Thread.id == thread_id).values(messages_fetched_at=datetime.now(UTC))
        )
        await session.commit()

    # Auto-revival "szeroki" (1:1): insert czegokolwiek + obecnosc dowolnej
    # cudzej wiadomosci w pobranej stronie historii -> done wraca do inbox.
    if inserted > 0:
        incoming_fresh = any(
            (record.get("sender") or {}).get("community_member_id") != jwt.community_member_id
            for record in response["records"]
        )
        if incoming_fresh:
            await revive_if_done(thread_id)

    log.debug(f"Thread {thread_id}: {len(response['records'])} messages, {inserted} new")
    if inserted > 0:
        broker.broadcast({"type": "messages:loaded", "threadId": thread_id, "count": inserted})
    return inserted


async def _upsert_message(thread_id: int, community_member_id: int, record: dict) -> bool:
    sender = record.get("sender") or {}
    sender_is_me = sender.get("community_member_id") == community_member_id
    plain = _pick_plain_text(record)

    atts = extract_attachments(record.get("rich_text_body"))
    has_voice = any(a.kind == "audio" and a.voice_message for a in atts)

    edited_at = _parse_date(record["edited_at"]) if record.get("edited_at") else None
    stmt = (
        pg_insert(Message)
        .values(
            thread_id=thread_id,
            circle_message_id=record["id"],
            body=plain,
            rich_text_body=record.get("rich_text_body"),
            sender_id=sender.get("community_member_id"),
            sender_name=sender.get("name"),
            sender_is_me=sender_is_me,
            parent_message_id=record.get("parent_message_id"),
            chat_thread_id=record.get("chat_thread_id"),
            created_at=_parse_date(record["created_at"]),
            edited_at=edited_at,
            # 'pending' tylko przy INSERT - przy konflikcie status zostaje.
            voice_transcript_status="pending" if has_voice else None,
        )
        .on_conflict_do_update(
            index_elements=["thread_id", "circle_message_id"],
            set_={
                "body": plain,
                "rich_text_body": record.get("rich_text_body"),
                "edited_at": edited_at,
            },
        )
        .returning(Message.id, text("(xmax = 0)"))
    )

    async with async_session_maker() as session:
        row = (await session.execute(stmt)).first()
        await session.commit()

        message_id = row.id if row is not None else None
        if message_id is not None:
            # attachment_index = indeks w PELNEJ liscie zalacznikow (nie wsrod
            # samych obrazkow) - od tego zalezy unikalnosc kolejki vision.
            image_rows = [
                {
                    "message_id": message_id,
                    "attachment_index": idx,
                    "attachment_url": a.full_url if a.full_url is not None else a.url,
                }
                for idx, a in enumerate(atts)
                if a.kind == "image"
            ]
            if image_rows:
                await session.execute(
                    pg_insert(MessageImageDescription).values(image_rows).on_conflict_do_nothing()
                )
                await session.commit()

    return row is not None and bool(row[1])


async def refetch_threads(thread_ids: list[int]) -> None:
    """Wymusza re-sync wiadomosci: nastepny tick zobaczy staleMessages=true."""
    if len(thread_ids) == 0:
        return
    async with async_session_maker() as session:
        await session.execute(
            update(Thread).where(Thread.id.in_(thread_ids)).values(messages_fetched_at=None)
        )
        await session.commit()
