"""Port services/compose-orchestrator.ts - pierwsza wiadomosc do nowego odbiorcy.

- generate: cold opener, bez streamingu WS, jednorazowy session-id uuid4,
- send: Circle find-or-create chat roomu, materializacja watku (select-then-
  -update/insert, nie ON CONFLICT - jak oryginal), audyt sent_messages dopiero
  PO sukcesie wysylki (inaczej niz send_draft!), placeholder circle_message_id
  = -epoch_ms z ON CONFLICT DO NOTHING. Zero eventow WS.
"""

import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.claude_cli import run_claude
from app.core.config import settings
from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.core.semaphore import Semaphore
from app.modules.circle_dm.circle.client import CircleApiError, send_to_new_recipient
from app.modules.circle_dm.circle.jwt_manager import get_jwt_for, invalidate_jwt
from app.modules.circle_dm.models import Account, Member, Message, SentMessage, Thread
from app.modules.circle_dm.services.app_settings import (
    compose_system_prompt,
    get_draft_model,
    get_global_meta_prompt,
)
from app.modules.circle_dm.services.knowledge_base import build_kb_block

log = create_logger("compose")
_sem = Semaphore(settings.CLAUDE_MAX_CONCURRENT)


async def _get_account_and_member(
    account_id: int, circle_community_member_id: int
) -> tuple[Account, Member]:
    async with async_session_maker() as session:
        account = (
            await session.execute(select(Account).where(Account.id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            raise Exception(f"admin_account {account_id} not found")

        member = (
            await session.execute(
                select(Member)
                .where(
                    and_(
                        Member.account_id == account_id,
                        Member.circle_community_member_id == circle_community_member_id,
                    )
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if member is None:
            raise Exception(
                f"member {circle_community_member_id} not cached for this account"
            )

    return account, member


async def _run_streaming(
    *, prompt: str, append_system_prompt: str | None, session_id: str, model: str | None
) -> dict:
    result = await run_claude(
        prompt,
        session_id=session_id,
        append_system_prompt=append_system_prompt,
        model=model,
    )
    if result.exit_code != 0:
        raise Exception(f"claude exited with code {result.exit_code}: {result.stderr[:500]}")
    if not result.text.strip():
        raise Exception("claude returned empty draft")
    return {
        "draft": result.text.strip(),
        "tokensUsed": result.tokens_used,
        "costUsd": result.cost_usd,
    }


async def generate_compose_draft(account_id: int, circle_community_member_id: int) -> dict:
    account, member = await _get_account_and_member(account_id, circle_community_member_id)

    profile_lines: list[str] = []
    if member.headline:
        profile_lines.append(f"- Headline: {member.headline}")
    if member.bio:
        profile_lines.append(f"- Bio: {member.bio}")
    if member.location:
        profile_lines.append(f"- Lokalizacja: {member.location}")
    if member.last_seen_text:
        profile_lines.append(f"- Status: {member.last_seen_text}")
    profile_block = (
        "Co wiemy o tej osobie:\n" + "\n".join(profile_lines) + "\n\n" if profile_lines else ""
    )

    base_prompt = (
        f'{profile_block}Wcielasz się w "{account.label}". '
        f"Wygeneruj PIERWSZĄ wiadomość DM do {member.name} na Circle. "
        "To jest cold opener — nigdy wcześniej z tą osobą nie pisaliśmy. "
        "Pisz po polsku, naturalnie, krótko (2-4 zdania), w pierwszej osobie, zgodnie z personą. "
        "Możesz nawiązać do tego co wiemy o tej osobie z profilu (jeśli sensowne), "
        "ale nie podlizuj się. "
        "Zwróć WYŁĄCZNIE treść wiadomości — bez prefiksu, bez wyjaśnień, "
        "bez cudzysłowów, bez bloków kodu."
    )

    kb_block = await build_kb_block(account_id)
    prompt = f"{kb_block}\n\n---\n\n{base_prompt}" if kb_block else base_prompt

    release = await _sem.acquire()
    try:
        meta_prompt = await get_global_meta_prompt()
        db_model = await get_draft_model()
        return await _run_streaming(
            prompt=prompt,
            append_system_prompt=compose_system_prompt(account.system_prompt, meta_prompt),
            session_id=str(uuid.uuid4()),
            model=db_model if db_model is not None else settings.DRAFT_MODEL,
        )
    finally:
        release()


async def send_compose_draft(
    account_id: int, circle_community_member_id: int, body: str
) -> dict:
    account, member = await _get_account_and_member(account_id, circle_community_member_id)
    jwt = await get_jwt_for(account_id)

    try:
        response = await send_to_new_recipient(
            jwt.access_token, [circle_community_member_id], body
        )
    except Exception as err:
        if isinstance(err, CircleApiError) and err.status == 401:
            await invalidate_jwt(account_id)
        message = str(err)
        log.error("compose send failed", message)
        return {"ok": False, "error": message}

    room = response["chat_room"]
    pinned_at_raw = room.get("pinned_at")

    thread_values = {
        "account_id": account_id,
        "circle_chat_room_id": room["id"],
        "circle_chat_room_uuid": room["uuid"],
        "chat_room_kind": room["chat_room_kind"],
        "chat_room_name": room.get("chat_room_name"),
        "other_participant_email": member.email,
        "other_participant_name": member.name,
        "other_participant_id": member.circle_community_member_id,
        "other_participant_avatar_url": member.avatar_url,
        "unread_messages_count": 0,
        "pinned_at": (
            datetime.fromisoformat(str(pinned_at_raw).replace("Z", "+00:00"))
            if pinned_at_raw
            else None
        ),
        "last_message_at": datetime.now(UTC),
        "last_message_sender_id": jwt.community_member_id,
        "last_message_sender_is_me": True,
        "last_message_preview": body[:240],
        "raw_payload": room,
        "fetched_at": datetime.now(UTC),
    }

    async with async_session_maker() as session:
        existing_id = (
            await session.execute(
                select(Thread.id)
                .where(
                    and_(
                        Thread.account_id == account_id,
                        Thread.circle_chat_room_uuid == room["uuid"],
                    )
                )
                .limit(1)
            )
        ).scalar_one_or_none()

        if existing_id is not None:
            await session.execute(
                update(Thread).where(Thread.id == existing_id).values(**thread_values)
            )
            thread_id = existing_id
        else:
            thread_id = (
                await session.execute(
                    insert(Thread).values(**thread_values).returning(Thread.id)
                )
            ).scalar_one()
        await session.commit()

        await session.execute(
            insert(SentMessage).values(
                thread_id=thread_id,
                body=body,
                circle_message_id=None,
                circle_creation_uuid=None,
                error=None,
            )
        )
        await session.commit()

        placeholder_id = -int(time.time() * 1000)
        await session.execute(
            pg_insert(Message)
            .values(
                thread_id=thread_id,
                circle_message_id=placeholder_id,
                body=body,
                rich_text_body=None,
                sender_id=jwt.community_member_id,
                sender_name=account.label,
                sender_is_me=True,
                parent_message_id=None,
                chat_thread_id=None,
                created_at=datetime.now(UTC),
                edited_at=None,
            )
            .on_conflict_do_nothing(
                index_elements=[Message.thread_id, Message.circle_message_id]
            )
        )
        await session.commit()

    log.info(
        f"compose sent to {member.name} (member {circle_community_member_id}) "
        f"→ thread {thread_id}"
    )
    return {"ok": True, "threadId": thread_id, "circleChatRoomUuid": room["uuid"]}
