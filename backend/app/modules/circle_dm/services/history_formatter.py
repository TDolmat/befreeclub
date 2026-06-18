"""Port services/history-formatter.ts - transkrypt watku DM dla Claude.

Naglowki [YYYY-MM-DD HH:mm] w strefie Europe/Warsaw, linie glosowek i zdjec
doslownie przez voice_format, stopka z nadawca ostatniej wiadomosci.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.db import async_session_maker
from app.modules.circle_dm.models import Account, Message, MessageImageDescription, Thread
from app.modules.circle_dm.services.voice_format import format_image_for_ai, format_voice_for_ai

_TZ = ZoneInfo("Europe/Warsaw")


def _format_ts(dt: datetime) -> str:
    return dt.astimezone(_TZ).strftime("%Y-%m-%d %H:%M")


async def format_thread_history_for_claude(thread_id: int) -> dict:
    async with async_session_maker() as session:
        thread = (
            await session.execute(
                select(Thread.id, Thread.account_id, Thread.other_participant_name)
                .where(Thread.id == thread_id)
                .limit(1)
            )
        ).first()
        if thread is None:
            raise Exception(f"thread {thread_id} not found")

        account = (
            await session.execute(
                select(Account.label, Account.email)
                .where(Account.id == thread.account_id)
                .limit(1)
            )
        ).first()

        messages = (
            (
                await session.execute(
                    select(Message)
                    .where(Message.thread_id == thread_id)
                    .order_by(Message.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

        admin_label = account.label if account is not None else "Ja"
        other_label = (
            thread.other_participant_name
            if thread.other_participant_name is not None
            else "Druga strona"
        )

        if len(messages) == 0:
            return {
                "history": "(Brak historii wiadomości — to pierwsze pisanie z tą osobą.)",
                "adminLabel": admin_label,
                "otherLabel": other_label,
                "hasMessages": False,
            }

        message_ids = [m.id for m in messages]
        desc_rows = (
            (
                await session.execute(
                    select(MessageImageDescription).where(
                        MessageImageDescription.message_id.in_(message_ids)
                    )
                )
            )
            .scalars()
            .all()
        )

    descs_by_msg: dict[int, list[MessageImageDescription]] = {}
    for d in desc_rows:
        descs_by_msg.setdefault(d.message_id, []).append(d)

    def who_for(msg: Message) -> str:
        if msg.sender_is_me:
            return f"{admin_label} (ja)"
        return msg.sender_name if msg.sender_name is not None else other_label

    lines: list[str] = []
    for msg in messages:
        lines.append(f"[{_format_ts(msg.created_at)}] {who_for(msg)}:")
        body = msg.body.strip()
        if body:
            lines.append(body)
        if msg.voice_transcript_status is not None:
            lines.append(
                format_voice_for_ai(
                    msg.voice_duration_sec, msg.voice_transcript_status, msg.voice_transcript
                )
            )
        descs = sorted(descs_by_msg.get(msg.id, []), key=lambda d: d.attachment_index)
        for d in descs:
            lines.append(format_image_for_ai(d.status, d.description))
        lines.append("")

    last = messages[-1]
    lines.append("---")
    lines.append(f"Ostatnia wiadomość jest od: {who_for(last)} ({_format_ts(last.created_at)})")

    return {
        "history": "\n".join(lines),
        "adminLabel": admin_label,
        "otherLabel": other_label,
        "hasMessages": True,
    }
