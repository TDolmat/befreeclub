"""Port core/auth/middleware.ts jako FastAPI dependency.

Dev (NODE_ENV != production): no-op, fake tozsamosc dev@local - lokalnie
nie trzeba sie logowac. Prod: cookie admin_session -> validate_session
(sliding window), brak/nieprawidlowa -> 401 {"error":"Unauthorized"}.
"""

from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.core.config import settings
from app.modules.admin.services.sessions import SESSION_COOKIE_NAME, validate_session


@dataclass(frozen=True)
class AuthContext:
    auth_account_id: int
    email: str


DEV_FAKE_AUTH = AuthContext(auth_account_id=0, email="dev@local")


async def require_auth(request: Request) -> AuthContext:
    if settings.NODE_ENV != "production":
        return DEV_FAKE_AUTH
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    session = await validate_session(session_id)
    if session is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return AuthContext(auth_account_id=session.auth_account_id, email=session.email)
