"""Port core/auth/sessions.ts. Sesje w admin.sessions (stare auth_sessions).

Sliding window: KAZDA udana walidacja przesuwa expires_at na now+30d
(write do DB na kazdy uwierzytelniony request, takze GET /api/auth/me).
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update

from app.core.db import async_session_maker
from app.modules.admin.models import Session, User

SESSION_COOKIE_NAME = "admin_session"
SESSION_TTL_MS = 30 * 24 * 60 * 60 * 1000


@dataclass
class SessionContext:
    auth_account_id: int
    email: str


def _new_session_id() -> str:
    return secrets.token_hex(32)


async def create_session(
    user_id: int, *, ip_addr: str | None, user_agent: str | None
) -> dict:
    session_id = _new_session_id()
    now = datetime.now(UTC)
    expires_at = now + timedelta(milliseconds=SESSION_TTL_MS)
    async with async_session_maker() as db:
        db.add(
            Session(
                id=session_id,
                user_id=user_id,
                expires_at=expires_at,
                last_seen_at=now,
                ip_addr=ip_addr,
                user_agent=user_agent,
            )
        )
        await db.commit()
    return {"id": session_id, "expires_at": expires_at}


async def validate_session(session_id: str) -> SessionContext | None:
    async with async_session_maker() as db:
        row = (
            await db.execute(
                select(Session.expires_at, Session.user_id, User.email)
                .join(User, User.id == Session.user_id)
                .where(Session.id == session_id)
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        expires_at, user_id, email = row

        now = datetime.now(UTC)
        if expires_at < now:
            # Wygasla - lazy cleanup.
            await db.execute(delete(Session).where(Session.id == session_id))
            await db.commit()
            return None

        await db.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(last_seen_at=now, expires_at=now + timedelta(milliseconds=SESSION_TTL_MS))
        )
        await db.commit()

    return SessionContext(auth_account_id=user_id, email=email)


async def invalidate_session(session_id: str) -> None:
    async with async_session_maker() as db:
        await db.execute(delete(Session).where(Session.id == session_id))
        await db.commit()


async def invalidate_all_for_account(user_id: int) -> None:
    async with async_session_maker() as db:
        await db.execute(delete(Session).where(Session.user_id == user_id))
        await db.commit()


async def purge_expired_sessions() -> int:
    """Wolane z workera co 1h (app/main.py); walidacja i tak odrzuca wygasle."""
    async with async_session_maker() as db:
        result = await db.execute(
            delete(Session)
            .where(Session.expires_at < datetime.now(UTC))
            .returning(Session.id)
        )
        deleted = result.all()
        await db.commit()
    return len(deleted)
