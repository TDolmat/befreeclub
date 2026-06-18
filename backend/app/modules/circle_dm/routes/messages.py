"""Port tools/circle-dm/routes/messages.ts (1:1 wg docs/spec/routes-a.md sekcja 4).
Montowane pod /api/circle-dm/messages (za require_auth).

Retry tylko kolejkuje (reset statusu + attempts) - transkrypcje/opisy robi
worker w swoim ticku. Retry opisu obrazka WYMAGA zgodnosci message_id
(w odroznieniu od check-upow).
"""

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.modules.circle_dm.models import Message, MessageImageDescription
from app.modules.circle_dm.services.image_description_worker import retry_image_description
from app.modules.circle_dm.services.voice_transcript_worker import retry_transcript

router = APIRouter()


@router.post("/{message_id}/transcribe-retry")
async def transcribe_retry(
    message_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_session),
):
    row = (
        await db.execute(
            select(Message.id, Message.voice_transcript_status)
            .where(Message.id == message_id)
            .limit(1)
        )
    ).first()
    if row is None:
        return JSONResponse({"error": "message not found"}, status_code=404)
    if row.voice_transcript_status is None:
        return JSONResponse({"error": "message has no voice attachment"}, status_code=400)
    await retry_transcript(message_id)
    return {"ok": True}


@router.post("/{message_id}/image-descriptions/{desc_id}/retry")
async def image_description_retry(
    message_id: int = Path(gt=0),
    desc_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_session),
):
    row = (
        await db.execute(
            select(MessageImageDescription.id)
            .where(
                MessageImageDescription.id == desc_id,
                MessageImageDescription.message_id == message_id,
            )
            .limit(1)
        )
    ).first()
    if row is None:
        return JSONResponse({"error": "image description not found"}, status_code=404)
    await retry_image_description(desc_id)
    return {"ok": True}
