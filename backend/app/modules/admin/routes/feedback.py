"""Port core/feedback/routes.ts. Montowane pod /api/feedback (za require_auth).

Wspolny backlog obu adminow - brak multi-tenancy. Quirki 1:1: PATCH/DELETE
bez sprawdzenia istnienia zawsze {"ok":true}; w dev lazy INSERT konta id=0
(inaczej FK przy fake-userze dev@local by sie wywalil).
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import case, delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.logging import create_logger, to_iso_string
from app.core.schemas import CamelModel
from app.modules.admin.models import FeedbackItem, User
from app.modules.admin.services.auth import AuthContext, require_auth

log = create_logger("routes:feedback")

router = APIRouter()


class CreateFeedbackBody(CamelModel):
    body: str = Field(min_length=1, max_length=4000)
    scope: str = Field(default="general", min_length=1, max_length=40)


class UpdateFeedbackStatusBody(CamelModel):
    status: Literal["open", "done"]


def _serialize_row(item: FeedbackItem, email: str | None) -> dict:
    return {
        "id": item.id,
        "authAccountId": item.user_id,
        "authorEmail": email,
        "scope": item.scope,
        "body": item.body,
        "status": item.status,
        "doneAt": to_iso_string(item.done_at) if item.done_at is not None else None,
        "createdAt": to_iso_string(item.created_at),
    }


async def _ensure_dev_auth_account(db: AsyncSession, auth_account_id: int) -> None:
    if settings.NODE_ENV == "production":
        return
    if auth_account_id != 0:
        return
    await db.execute(
        text(
            "INSERT INTO admin.users (id, email, password_hash) "
            "VALUES (0, 'dev@local', '') "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    await db.commit()


@router.get("")
async def list_feedback(db: AsyncSession = Depends(get_session)) -> dict:
    rows = (
        await db.execute(
            select(FeedbackItem, User.email)
            .outerjoin(User, User.id == FeedbackItem.user_id)
            .order_by(
                case((FeedbackItem.status == "open", 0), else_=1),
                FeedbackItem.created_at.desc(),
            )
        )
    ).all()
    return {"items": [_serialize_row(item, email) for item, email in rows]}


@router.get("/count")
async def open_count(db: AsyncSession = Depends(get_session)) -> dict:
    count = (
        await db.execute(
            select(func.count()).select_from(FeedbackItem).where(FeedbackItem.status == "open")
        )
    ).scalar_one()
    return {"openCount": count}


@router.post("")
async def create_feedback(
    payload: CreateFeedbackBody,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    await _ensure_dev_auth_account(db, auth.auth_account_id)
    inserted = (
        await db.execute(
            insert(FeedbackItem)
            .values(user_id=auth.auth_account_id, body=payload.body, scope=payload.scope)
            .returning(FeedbackItem)
        )
    ).scalars().one()
    await db.commit()
    log.info(f"feedback {inserted.id} from {auth.email} ({payload.scope})")
    return JSONResponse({"item": _serialize_row(inserted, auth.email)}, status_code=201)


@router.patch("/{item_id}/status")
async def update_feedback_status(
    payload: UpdateFeedbackStatusBody,
    item_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_session),
) -> dict:
    now = datetime.now(UTC)
    await db.execute(
        update(FeedbackItem)
        .where(FeedbackItem.id == item_id)
        .values(
            status=payload.status,
            done_at=now if payload.status == "done" else None,
            updated_at=now,
        )
    )
    await db.commit()
    return {"ok": True}


@router.delete("/{item_id}")
async def delete_feedback(
    item_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_session),
) -> dict:
    await db.execute(delete(FeedbackItem).where(FeedbackItem.id == item_id))
    await db.commit()
    return {"ok": True}
