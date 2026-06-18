"""Port core/auth/routes.ts. Montowane pod /api/auth (publiczne).

Dev bypass: login akceptuje wszystko (po walidacji body) i NIE ustawia cookie,
/me zawsze dev@local. Anty-enumeracja: dummy scrypt liczony zawsze gdy konta
brak, zeby timing "brak emaila" == "zle haslo".
"""

import math

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.logging import create_logger
from app.core.security import DUMMY_HASH, verify_password_async
from app.modules.admin.models import User
from app.modules.admin.schemas import LoginRequest
from app.modules.admin.services.auth import DEV_FAKE_AUTH
from app.modules.admin.services.rate_limit import (
    client_ip,
    is_locked,
    record_failure,
    record_success,
)
from app.modules.admin.services.sessions import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_MS,
    create_session,
    invalidate_session,
    validate_session,
)

log = create_logger("auth")

router = APIRouter()


def _set_session_cookie(response: JSONResponse, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        secure=settings.NODE_ENV == "production",
        samesite="lax",
        path="/",
        max_age=SESSION_TTL_MS // 1000,
    )


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    if settings.NODE_ENV != "production":
        return JSONResponse({"ok": True, "email": DEV_FAKE_AUTH.email})

    email_norm = payload.email.lower()
    ip = client_ip(request)
    user_agent = request.headers.get("user-agent")

    bucket_key = f"{email_norm}|{ip}"
    lock = is_locked(bucket_key)
    if lock["locked"]:
        minutes = math.ceil((lock.get("retry_after_sec") or 0) / 60)
        return JSONResponse(
            {"error": f"Too many attempts. Try again in ~{minutes} min."}, status_code=429
        )

    account = (
        await db.execute(select(User).where(User.email == email_norm).limit(1))
    ).scalar_one_or_none()

    ok_hash = account.password_hash if account else DUMMY_HASH
    password_ok = await verify_password_async(payload.password, ok_hash)

    if not account or not password_ok:
        failure = record_failure(bucket_key)
        locked_str = "true" if failure["locked_now"] else "false"
        log.warn(f"failed login {email_norm} from {ip} (locked: {locked_str})")
        if failure["locked_now"]:
            minutes = math.ceil((failure.get("retry_after_sec") or 0) / 60)
            return JSONResponse(
                {"error": f"Too many attempts. Locked for ~{minutes} min."}, status_code=429
            )
        return JSONResponse({"error": "Invalid email or password"}, status_code=401)

    record_success(bucket_key)

    created = await create_session(account.id, ip_addr=ip, user_agent=user_agent)
    response = JSONResponse({"ok": True, "email": account.email})
    _set_session_cookie(response, created["id"])
    log.info(f"login ok {email_norm} from {ip}")
    return response


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        await invalidate_session(session_id)
        log.info("logout")
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@router.get("/me")
async def me(request: Request) -> JSONResponse:
    if settings.NODE_ENV != "production":
        return JSONResponse({"authenticated": True, "email": DEV_FAKE_AUTH.email})
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return JSONResponse({"authenticated": False})
    session = await validate_session(session_id)
    if session is None:
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return response
    return JSONResponse({"authenticated": True, "email": session.email})
