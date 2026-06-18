"""Port routes/drafts.ts. Montowane pod /api/circle-dm/drafts (za require_auth).

:id w KAZDYM endpointcie to threadId (wiersz threads), nie id sesji draftu.
POST /:id/generate = fire-and-forget: 200 {ok:true} natychmiast, bledy tla
POLYKANE bez sladu w HTTP (quirk oryginalu), postep idzie po WS.
"""

import asyncio

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.logging import to_iso_string
from app.modules.circle_dm.models import DraftIteration, DraftSession
from app.modules.circle_dm.schemas import SendDraftRequest, UpdateDraftRequest
from app.modules.circle_dm.services.draft_orchestrator import (
    generate_initial_draft,
    reset_draft,
    set_draft,
)
from app.modules.circle_dm.services.send import send_draft

router = APIRouter()

_background: set[asyncio.Task[None]] = set()


async def _generate_swallowing_errors(thread_id: int) -> None:
    # Odpowiednik void generateInitialDraft(id).catch(() => {}).
    try:
        await generate_initial_draft(thread_id)
    except Exception:
        pass


@router.get("/{id}")
async def get_draft_state(
    id: int = Path(gt=0), db: AsyncSession = Depends(get_session)
) -> dict:
    session = (
        await db.execute(select(DraftSession).where(DraftSession.thread_id == id).limit(1))
    ).scalar_one_or_none()
    if session is None:
        return {"session": None, "iterations": []}

    iterations = (
        (
            await db.execute(
                select(DraftIteration)
                .where(DraftIteration.draft_session_id == session.id)
                .order_by(DraftIteration.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "session": {
            "id": session.id,
            "threadId": session.thread_id,
            "claudeSessionId": session.claude_session_id,
            "status": session.status,
            "currentDraft": session.current_draft,
            "iterationsCount": session.iterations_count,
            "lastError": session.last_error,
            "createdAt": to_iso_string(session.created_at),
            "updatedAt": to_iso_string(session.updated_at),
        },
        "iterations": [
            {
                "id": i.id,
                "draftSessionId": i.draft_session_id,
                "iterationKind": i.iteration_kind,
                "userInstruction": i.user_instruction,
                "draftText": i.draft_text,
                "tokensUsed": i.tokens_used,
                "costUsd": float(i.cost_usd) if i.cost_usd is not None else None,
                "createdAt": to_iso_string(i.created_at),
            }
            for i in iterations
        ],
    }


@router.post("/{id}/generate")
async def generate(id: int = Path(gt=0)) -> dict:
    task = asyncio.create_task(_generate_swallowing_errors(id))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"ok": True}


@router.patch("/{id}")
async def update_draft(payload: UpdateDraftRequest, id: int = Path(gt=0)) -> dict:
    await set_draft(id, payload.draft)
    return {"ok": True}


@router.delete("/{id}")
async def delete_draft(id: int = Path(gt=0)) -> dict:
    await reset_draft(id)
    return {"ok": True}


@router.post("/{id}/send")
async def send(payload: SendDraftRequest, id: int = Path(gt=0)) -> JSONResponse:
    result = await send_draft(id, payload.body)
    return JSONResponse(result, status_code=200 if result["ok"] else 502)
