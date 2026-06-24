"""Port tools/circle-dm/services/voice-transcript-worker.ts (1:1 wg
docs/spec/services-media.md sekcja 5).

Kolejka = DB: wiersze messages z voice_transcript_status='pending' i attempts < 3.
Przetwarzanie szeregowe (batch 5 co tick), bez backoffu - retry to pozostawienie
statusu 'pending' do nastepnego ticka.
"""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select, update

from app.core.config import settings
from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.core.ws import broker
from app.modules.admin.services import secrets, settings_catalog
from app.modules.circle_dm.circle.attachments import extract_attachments
from app.modules.circle_dm.models import Message
from app.modules.circle_dm.services.openai_stt import (
    SttApiError,
    SttConfigError,
    SttFetchError,
    transcribe_audio_from_url,
)

log = create_logger("voice-worker")

MAX_ATTEMPTS = 3
BATCH_SIZE = 5

_task: asyncio.Task[None] | None = None
_running = False


async def _tick() -> None:
    global _running
    if _running:
        return
    if not secrets.resolve_sync("openai.api_key", env_fallback=True):
        return
    _running = True
    try:
        async with async_session_maker() as session:
            rows = (
                await session.execute(
                    select(
                        Message.id,
                        Message.thread_id,
                        Message.rich_text_body,
                        Message.voice_transcript_attempts,
                    )
                    .where(
                        Message.voice_transcript_status == "pending",
                        Message.voice_transcript_attempts < MAX_ATTEMPTS,
                    )
                    .order_by(Message.id.asc())
                    .limit(BATCH_SIZE)
                )
            ).all()
        for row in rows:
            await _transcribe_one(
                row.id, row.thread_id, row.rich_text_body, row.voice_transcript_attempts
            )
    except Exception as err:
        log.error(f"tick failed: {err}")
    finally:
        _running = False


async def _transcribe_one(
    message_id: int, thread_id: int, rich_text_body: object, prev_attempts: int
) -> None:
    atts = extract_attachments(rich_text_body)
    voice = next((a for a in atts if a.kind == "audio" and a.voice_message), None)
    if voice is None:
        # Brak glosowki po re-checku - terminalny error, zeby nie loopowac.
        async with async_session_maker() as session:
            await session.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(
                    voice_transcript_status="error",
                    voice_transcript_error="no voice attachment found in rich_text_body",
                    voice_transcript_attempts=prev_attempts + 1,
                )
            )
            await session.commit()
        return

    try:
        result = await transcribe_audio_from_url(voice.url, filename=voice.filename)
        text = result["text"]
        duration_sec = result["durationSec"]
        language = result["language"]
        async with async_session_maker() as session:
            await session.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(
                    voice_transcript=text,
                    voice_transcript_status="done",
                    voice_transcript_error=None,
                    voice_transcript_attempts=prev_attempts + 1,
                    voice_duration_sec=duration_sec,
                    voice_transcribed_at=datetime.now(UTC),
                )
            )
            await session.commit()
        log.info(
            f"msg {message_id} transcribed "
            f"({duration_sec if duration_sec is not None else '?'}s, "
            f"lang={language if language is not None else '?'}, {len(text)} chars)"
        )
        broker.broadcast(
            {"type": "message:transcript_ready", "threadId": thread_id, "messageId": message_id}
        )
    except Exception as err:
        attempts = prev_attempts + 1
        message = str(err)
        is_last = attempts >= MAX_ATTEMPTS
        # Blad konfiguracji nie pali budzetu prob - wyjscie bez UPDATE.
        if isinstance(err, SttConfigError):
            log.warn(f"msg {message_id}: {message} (no retry, config issue)")
            return
        fatal = isinstance(err, SttApiError) and 400 <= err.status < 500 and err.status != 429
        async with async_session_maker() as session:
            await session.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(
                    voice_transcript_status="error" if (is_last or fatal) else "pending",
                    voice_transcript_error=message[:500],
                    voice_transcript_attempts=attempts,
                )
            )
            await session.commit()
        log.warn(
            f"msg {message_id} attempt {attempts}/{MAX_ATTEMPTS} failed: {message}"
            + (" (fatal, no retry)" if fatal else "")
        )
        if isinstance(err, SttFetchError) and is_last:
            log.warn(f"msg {message_id}: signed URL likely expired")


async def _loop() -> None:
    while True:
        await _tick()
        interval_ms = await settings_catalog.effective("voiceTranscriptIntervalMs")
        await asyncio.sleep(interval_ms / 1000)


def start_voice_transcript_worker() -> None:
    global _task
    if _task:
        return
    # Worker startuje zawsze. Guard w _tick decyduje per tick przez resolve_sync,
    # dzieki czemu klucz ustawiony w panelu dziala bez restartu (a brak klucza =
    # tick no-op, bez palenia zasobow).
    log.info(
        f"Starting voice transcript worker (interval {settings.VOICE_TRANSCRIPT_INTERVAL_MS}ms)"
    )
    _task = asyncio.create_task(_loop())


def stop_voice_transcript_worker() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None


async def retry_transcript(message_id: int) -> None:
    """Reczne ponowne zakolejkowanie - pelny reset budzetu 3 prob.

    Nie czysci voice_transcript ani voice_duration_sec (nadpisze je sukces).
    """
    async with async_session_maker() as session:
        await session.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(
                voice_transcript_status="pending",
                voice_transcript_error=None,
                voice_transcript_attempts=0,
            )
        )
        await session.commit()
