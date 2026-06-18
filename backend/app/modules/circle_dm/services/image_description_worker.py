"""Port tools/circle-dm/services/image-description-worker.ts (1:1 wg
docs/spec/services-media.md sekcja 6).

Lustrzany do voice_transcript_worker - kolejka = wiersze message_image_descriptions
ze statusem 'pending' i attempts < 3, batch 5 co tick, szeregowo.
"""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select, update

from app.core.config import settings
from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.core.ws import broker
from app.modules.circle_dm.models import Message, MessageImageDescription
from app.modules.circle_dm.services.openai_vision import (
    VisionApiError,
    VisionConfigError,
    describe_image_from_url,
)

log = create_logger("image-worker")

MAX_ATTEMPTS = 3
BATCH_SIZE = 5

_task: asyncio.Task[None] | None = None
_running = False


async def _tick() -> None:
    global _running
    if _running:
        return
    if not settings.OPENAI_API_KEY:
        return
    _running = True
    try:
        async with async_session_maker() as session:
            rows = (
                await session.execute(
                    select(
                        MessageImageDescription.id,
                        MessageImageDescription.message_id,
                        MessageImageDescription.attachment_url,
                        MessageImageDescription.attempts,
                        Message.thread_id,
                    )
                    .join(Message, Message.id == MessageImageDescription.message_id)
                    .where(
                        MessageImageDescription.status == "pending",
                        MessageImageDescription.attempts < MAX_ATTEMPTS,
                    )
                    .order_by(MessageImageDescription.id.asc())
                    .limit(BATCH_SIZE)
                )
            ).all()
        for row in rows:
            await _describe_one(
                row.id, row.message_id, row.thread_id, row.attachment_url, row.attempts
            )
    except Exception as err:
        log.error(f"tick failed: {err}")
    finally:
        _running = False


async def _describe_one(
    desc_id: int, message_id: int, thread_id: int, url: str, prev_attempts: int
) -> None:
    try:
        result = await describe_image_from_url(url)
        description = result["description"]
        async with async_session_maker() as session:
            await session.execute(
                update(MessageImageDescription)
                .where(MessageImageDescription.id == desc_id)
                .values(
                    description=description,
                    status="done",
                    error=None,
                    attempts=prev_attempts + 1,
                    described_at=datetime.now(UTC),
                )
            )
            await session.commit()
        log.info(f"desc {desc_id} (msg {message_id}) done ({len(description)} chars)")
        broker.broadcast(
            {
                "type": "message:image_description_ready",
                "threadId": thread_id,
                "messageId": message_id,
            }
        )
    except Exception as err:
        attempts = prev_attempts + 1
        message = str(err)
        is_last = attempts >= MAX_ATTEMPTS
        if isinstance(err, VisionConfigError):
            log.warn(f"desc {desc_id}: {message} (no retry)")
            return
        fatal = isinstance(err, VisionApiError) and 400 <= err.status < 500 and err.status != 429
        async with async_session_maker() as session:
            await session.execute(
                update(MessageImageDescription)
                .where(MessageImageDescription.id == desc_id)
                .values(
                    status="error" if (is_last or fatal) else "pending",
                    error=message[:500],
                    attempts=attempts,
                )
            )
            await session.commit()
        log.warn(
            f"desc {desc_id} attempt {attempts}/{MAX_ATTEMPTS} failed: {message}"
            + (" (fatal, no retry)" if fatal else "")
        )


async def _loop() -> None:
    while True:
        await _tick()
        await asyncio.sleep(settings.IMAGE_DESCRIPTION_INTERVAL_MS / 1000)


def start_image_description_worker() -> None:
    global _task
    if _task:
        return
    if not settings.OPENAI_API_KEY:
        log.warn("OPENAI_API_KEY not set — image descriptions disabled")
        return
    log.info(
        f"Starting image description worker (interval {settings.IMAGE_DESCRIPTION_INTERVAL_MS}ms)"
    )
    _task = asyncio.create_task(_loop())


def stop_image_description_worker() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None


async def retry_image_description(desc_id: int) -> None:
    """Reset jednego wiersza opisu (po id wiersza opisu, nie wiadomosci)."""
    async with async_session_maker() as session:
        await session.execute(
            update(MessageImageDescription)
            .where(MessageImageDescription.id == desc_id)
            .values(status="pending", error=None, attempts=0)
        )
        await session.commit()
