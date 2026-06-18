"""Port circle/jwt-manager.ts - cache member JWT per konto w circle_dm.accounts.

Stan tokena zyje w DB (przezywa restart). In-memory jest TYLKO deduplikacja
inflight per account id. Refresh token jest zapisywany, ale NIGDY nie uzywany -
odswiezanie zawsze przez exchange admin tokena.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.modules.circle_dm.circle.client import exchange_admin_token_for_jwt
from app.modules.circle_dm.models import Account

log = create_logger("circle:jwt")

REFRESH_LEAD_MS = 5 * 60 * 1000


@dataclass
class JwtState:
    access_token: str
    expires_at: datetime
    community_id: int
    community_member_id: int


_inflight: dict[int, asyncio.Task[JwtState]] = {}


async def get_jwt_for(account_id: int) -> JwtState:
    existing = _inflight.get(account_id)
    if existing is not None:
        return await existing

    task = asyncio.create_task(_resolve_jwt(account_id))
    _inflight[account_id] = task
    task.add_done_callback(lambda _: _inflight.pop(account_id, None))
    return await task


async def _resolve_jwt(account_id: int) -> JwtState:
    # Trzy niezalezne kroki jak w TS (jwt-manager.ts): SELECT, HTTP do Circle
    # BEZ trzymanej sesji DB, osobna sesja na UPDATE. Sesja nie moze obejmowac
    # wywolania HTTP (do 30 s) - trzymalaby polaczenie z puli "idle in transaction".
    async with async_session_maker() as session:
        result = await session.execute(select(Account).where(Account.id == account_id).limit(1))
        account = result.scalar_one_or_none()

    if account is None:
        raise Exception(f"admin_account {account_id} not found")
    if not account.is_active:
        raise Exception(f"admin_account {account_id} is not active")

    now = datetime.now(UTC)
    if (
        account.circle_access_token
        and account.circle_access_token_expires_at is not None
        and account.circle_access_token_expires_at - timedelta(milliseconds=REFRESH_LEAD_MS) > now
        and account.community_id is not None
        and account.community_member_id is not None
    ):
        return JwtState(
            access_token=account.circle_access_token,
            expires_at=account.circle_access_token_expires_at,
            community_id=account.community_id,
            community_member_id=account.community_member_id,
        )

    log.info(f"Exchanging admin token for fresh JWT (account {account_id})")
    response = await exchange_admin_token_for_jwt(account.circle_admin_token, account.email)
    expires_at = datetime.fromisoformat(response["access_token_expires_at"].replace("Z", "+00:00"))

    async with async_session_maker() as session:
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                circle_access_token=response["access_token"],
                circle_access_token_expires_at=expires_at,
                circle_refresh_token=response["refresh_token"],
                community_id=response["community_id"],
                community_member_id=response["community_member_id"],
            )
        )
        await session.commit()

    return JwtState(
        access_token=response["access_token"],
        expires_at=expires_at,
        community_id=response["community_id"],
        community_member_id=response["community_member_id"],
    )


async def invalidate_jwt(account_id: int) -> None:
    """Po 401 z Circle: zeruje TYLKO access token + expiry, refresh token
    i community_id/community_member_id zostaja."""
    async with async_session_maker() as session:
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(circle_access_token=None, circle_access_token_expires_at=None)
        )
        await session.commit()
