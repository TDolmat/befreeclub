"""Port tools/circle-dm/routes/threads.ts (1:1 wg docs/spec/routes-a.md sekcja 3).
Montowane pod /api/circle-dm/threads (za require_auth).

Quirk 1:1: GET /:id/messages ma side-effecty (sync z Circle, auto-revival
done->inbox, delete placeholderow, kolejkowanie transkrypcji/opisow,
WS messages:loaded).

Swiadome naprawy quirkow oryginalu (docs/spec/port-odstepstwa.md):
- sort=next_checkup sortowany w SQL PRZED LIMIT (oryginal sortowal w JS po
  obcieciu limitem na sortowaniu "recent"),
- checkup done/delete scoped do :id watku z URL (oryginal ignorowal),
- PATCH status/flag zwraca 404 dla nieistniejacego watku (oryginal {ok:true}).
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.logging import to_iso_string
from app.modules.circle_dm.circle.attachments import NormalizedAttachment, extract_attachments
from app.modules.circle_dm.models import Checkup, Message, MessageImageDescription, Thread
from app.modules.circle_dm.schemas import (
    BulkActionBody,
    CreateCheckupBody,
    ThreadFlagBody,
    ThreadStatusBody,
    parse_zod_datetime,
)
from app.modules.circle_dm.services import thread_state
from app.modules.circle_dm.services.app_settings import get_settings
from app.modules.circle_dm.services.thread_sync import sync_messages_for_thread

router = APIRouter()

ThreadFilter = Literal["inbox", "unread", "no_reply", "silent", "flagged", "checkup", "done"]
ThreadSort = Literal["recent", "oldest_no_reply", "next_checkup"]

# Watek "zaparkowany" na PRZYSZLY check-up - chowany z Inbox. Check-up juz DUE
# (due_at <= now()) celowo NIE chowa watku - ma wrocic do Inbox z badge "DUE".
_future_pending_checkup_exists = exists(
    select(1).where(
        Checkup.thread_id == Thread.id,
        Checkup.done_at.is_(None),
        Checkup.due_at > func.now(),
    )
)

_any_pending_checkup_exists = exists(
    select(1).where(
        Checkup.thread_id == Thread.id,
        Checkup.done_at.is_(None),
    )
)


async def _load_pending_checkups(db: AsyncSession, thread_ids: list[int]) -> dict[int, dict]:
    """Pierwszy pending (due_at ASC) per watek daje nextDueAt/nextNote, count = liczba."""
    out: dict[int, dict] = {}
    if not thread_ids:
        return out
    rows = (
        await db.execute(
            select(Checkup.thread_id, Checkup.due_at, Checkup.note)
            .where(Checkup.thread_id.in_(thread_ids), Checkup.done_at.is_(None))
            .order_by(Checkup.due_at.asc())
        )
    ).all()
    for r in rows:
        agg = out.get(r.thread_id)
        if agg is not None:
            agg["count"] += 1
        else:
            out[r.thread_id] = {"next_due_at": r.due_at, "next_note": r.note, "count": 1}
    return out


def _serialize_thread(row: Thread, agg: dict | None) -> dict:
    return {
        "id": row.id,
        "adminAccountId": row.account_id,
        "circleChatRoomId": row.circle_chat_room_id,
        "circleChatRoomUuid": row.circle_chat_room_uuid,
        "chatRoomKind": row.chat_room_kind,
        "chatRoomName": row.chat_room_name,
        "otherParticipantEmail": row.other_participant_email,
        "otherParticipantName": row.other_participant_name,
        "otherParticipantId": row.other_participant_id,
        "otherParticipantAvatarUrl": row.other_participant_avatar_url,
        "unreadMessagesCount": row.unread_messages_count,
        "pinnedAt": to_iso_string(row.pinned_at) if row.pinned_at is not None else None,
        "status": row.status,
        "isFlagged": row.is_flagged,
        "nextCheckupDueAt": to_iso_string(agg["next_due_at"]) if agg is not None else None,
        "nextCheckupNote": agg["next_note"] if agg is not None else None,
        "pendingCheckupCount": agg["count"] if agg is not None else 0,
        "lastMessageAt": to_iso_string(row.last_message_at)
        if row.last_message_at is not None
        else None,
        "lastMessageSenderId": row.last_message_sender_id,
        "lastMessageSenderIsMe": row.last_message_sender_is_me,
        "lastMessagePreview": row.last_message_preview,
        "fetchedAt": to_iso_string(row.fetched_at),
    }


def _serialize_attachment(a: NormalizedAttachment) -> dict:
    return {
        "kind": a.kind,
        "url": a.url,
        "thumbnailUrl": a.thumbnail_url,
        "fullUrl": a.full_url,
        "filename": a.filename,
        "contentType": a.content_type,
        "byteSize": a.byte_size,
        "width": a.width,
        "height": a.height,
        "voiceMessage": a.voice_message,
    }


@router.get("")
async def list_threads(
    admin_account_id: int = Query(alias="adminAccountId", gt=0),
    filter: ThreadFilter = Query("inbox"),
    sort: ThreadSort = Query("recent"),
    limit: int = Query(100, ge=1, le=200),
    db: AsyncSession = Depends(get_session),
) -> dict:
    app_settings = await get_settings()
    no_reply_ms = app_settings.no_reply_threshold_days * 24 * 60 * 60 * 1000
    silence_ms = app_settings.silence_threshold_days * 24 * 60 * 60 * 1000

    conditions = [Thread.account_id == admin_account_id]

    if filter == "inbox":
        # Aktywne watki niezaparkowane na przyszly check-up.
        conditions.append(Thread.status == "inbox")
        conditions.append(~_future_pending_checkup_exists)
    elif filter == "unread":
        conditions.append(Thread.status == "inbox")
        conditions.append(Thread.last_message_sender_is_me.is_(False))
    elif filter == "no_reply":
        conditions.append(Thread.status == "inbox")
        conditions.append(Thread.last_message_sender_is_me.is_(True))
        conditions.append(
            Thread.last_message_at < datetime.now(UTC) - timedelta(milliseconds=no_reply_ms)
        )
    elif filter == "silent":
        # Cisza obustronna: nic od nikogo przez N dni. Pomija done (juz
        # zarchiwizowane) i watki zaparkowane na przyszly check-up.
        conditions.append(Thread.status == "inbox")
        conditions.append(
            Thread.last_message_at < datetime.now(UTC) - timedelta(milliseconds=silence_ms)
        )
        conditions.append(~_future_pending_checkup_exists)
    elif filter == "flagged":
        # Przekrojowy - lapie tez oflagowane done.
        conditions.append(Thread.is_flagged.is_(True))
    elif filter == "checkup":
        conditions.append(_any_pending_checkup_exists)
    elif filter == "done":
        conditions.append(Thread.status == "done")

    if sort == "oldest_no_reply":
        order_by = [Thread.pinned_at.desc(), Thread.last_message_at.asc()]
    elif sort == "next_checkup":
        # Najblizszy pending check-up per watek, NULL-e na koncu, tiebreak jak
        # "recent" - sortowanie w SQL PRZED LIMIT (naprawa quirka oryginalu,
        # ktory sortowal w JS po obcieciu limitem).
        next_due = (
            select(func.min(Checkup.due_at))
            .where(Checkup.thread_id == Thread.id, Checkup.done_at.is_(None))
            .correlate(Thread)
            .scalar_subquery()
        )
        order_by = [
            next_due.asc().nulls_last(),
            Thread.pinned_at.desc(),
            Thread.last_message_at.desc(),
        ]
    else:
        order_by = [Thread.pinned_at.desc(), Thread.last_message_at.desc()]

    rows = (
        (await db.execute(select(Thread).where(*conditions).order_by(*order_by).limit(limit)))
        .scalars()
        .all()
    )

    checkups_by_thread = await _load_pending_checkups(db, [r.id for r in rows])
    threads = [_serialize_thread(r, checkups_by_thread.get(r.id)) for r in rows]

    return {"threads": threads, "count": len(threads)}


@router.get("/{thread_id}")
async def get_thread(
    thread_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_session),
):
    row = (
        await db.execute(select(Thread).where(Thread.id == thread_id).limit(1))
    ).scalar_one_or_none()
    if row is None:
        return JSONResponse({"error": "thread not found"}, status_code=404)
    checkups = await _load_pending_checkups(db, [thread_id])
    return _serialize_thread(row, checkups.get(thread_id))


@router.patch("/{thread_id}/status")
async def patch_thread_status(
    payload: ThreadStatusBody,
    thread_id: int = Path(gt=0),
):
    found = await thread_state.set_thread_status(thread_id, payload.status)
    if not found:
        return JSONResponse({"error": "thread not found"}, status_code=404)
    return {"ok": True}


@router.patch("/{thread_id}/flag")
async def patch_thread_flag(
    payload: ThreadFlagBody,
    thread_id: int = Path(gt=0),
):
    found = await thread_state.set_thread_flagged(thread_id, payload.is_flagged)
    if not found:
        return JSONResponse({"error": "thread not found"}, status_code=404)
    return {"ok": True}


# Bulk przypisanie folderu/flagi z listy inbox (multi-select -> akcja).
@router.post("/bulk-action")
async def bulk_action(payload: BulkActionBody) -> dict:
    if payload.action == "done":
        count = await thread_state.bulk_set_status(payload.admin_account_id, payload.ids, "done")
    elif payload.action == "inbox":
        count = await thread_state.bulk_set_status(payload.admin_account_id, payload.ids, "inbox")
    elif payload.action == "flag":
        count = await thread_state.bulk_set_flagged(payload.admin_account_id, payload.ids, True)
    else:
        count = await thread_state.bulk_set_flagged(payload.admin_account_id, payload.ids, False)
    return {"ok": True, "count": count}


@router.get("/{thread_id}/checkups")
async def list_thread_checkups(thread_id: int = Path(gt=0)) -> dict:
    return {"checkups": await thread_state.list_checkups(thread_id)}


@router.post("/{thread_id}/checkups")
async def create_thread_checkup(
    payload: CreateCheckupBody,
    thread_id: int = Path(gt=0),
) -> dict:
    # 200 (nie 201), goly CheckupRow bez koperty - 1:1 z oryginalem.
    return await thread_state.create_checkup(
        thread_id, parse_zod_datetime(payload.due_at), payload.note
    )


@router.patch("/{thread_id}/checkups/{checkup_id}/done")
async def mark_thread_checkup_done(
    thread_id: int = Path(gt=0),
    checkup_id: int = Path(gt=0),
) -> dict:
    # Celowo {ok:true} takze przy 0 dopasowan: front nie ma onError, a refetch
    # z onSuccess samoleczy stale UI. Scoping do thread_id = naprawa quirka.
    await thread_state.mark_checkup_done(thread_id, checkup_id)
    return {"ok": True}


@router.delete("/{thread_id}/checkups/{checkup_id}")
async def delete_thread_checkup(
    thread_id: int = Path(gt=0),
    checkup_id: int = Path(gt=0),
) -> dict:
    await thread_state.delete_checkup(thread_id, checkup_id)
    return {"ok": True}


@router.get("/{thread_id}/messages")
async def list_thread_messages(
    request: Request,
    thread_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_session),
):
    # refetch czytany recznie - kazda wartosc poza '1' znaczy false (1:1).
    refetch = request.query_params.get("refetch") == "1"
    thread = (
        await db.execute(
            select(Thread.id, Thread.messages_fetched_at).where(Thread.id == thread_id).limit(1)
        )
    ).first()
    if thread is None:
        return JSONResponse({"error": "thread not found"}, status_code=404)

    if refetch or thread.messages_fetched_at is None:
        try:
            await sync_messages_for_thread(thread_id)
        except Exception as err:
            return JSONResponse({"error": f"Circle fetch failed: {err}"}, status_code=502)

    rows = (
        (
            await db.execute(
                select(Message)
                .where(Message.thread_id == thread_id)
                .order_by(Message.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    message_ids = [m.id for m in rows]
    image_desc_rows = (
        (
            await db.execute(
                select(
                    MessageImageDescription.id,
                    MessageImageDescription.message_id,
                    MessageImageDescription.attachment_index,
                    MessageImageDescription.description,
                    MessageImageDescription.status,
                    MessageImageDescription.error,
                ).where(MessageImageDescription.message_id.in_(message_ids))
            )
        ).all()
        if message_ids
        else []
    )
    desc_by_msg: dict[int, list] = {}
    for d in image_desc_rows:
        desc_by_msg.setdefault(d.message_id, []).append(d)

    return {
        "messages": [
            {
                "id": m.id,
                "threadId": m.thread_id,
                "circleMessageId": m.circle_message_id,
                "body": m.body,
                "senderId": m.sender_id,
                "senderName": m.sender_name,
                "senderIsMe": m.sender_is_me,
                "parentMessageId": m.parent_message_id,
                "chatThreadId": m.chat_thread_id,
                "createdAt": to_iso_string(m.created_at),
                "editedAt": to_iso_string(m.edited_at) if m.edited_at is not None else None,
                "attachments": [
                    _serialize_attachment(a) for a in extract_attachments(m.rich_text_body)
                ],
                "voiceTranscript": m.voice_transcript,
                "voiceTranscriptStatus": m.voice_transcript_status,
                "voiceTranscriptError": m.voice_transcript_error,
                "voiceDurationSec": m.voice_duration_sec,
                "imageDescriptions": [
                    {
                        "id": d.id,
                        "attachmentIndex": d.attachment_index,
                        "description": d.description,
                        "status": d.status,
                        "error": d.error,
                    }
                    for d in sorted(desc_by_msg.get(m.id, []), key=lambda d: d.attachment_index)
                ],
            }
            for m in rows
        ],
        "hasPrevious": False,
        "hasNext": False,
    }
