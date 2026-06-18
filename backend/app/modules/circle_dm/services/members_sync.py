"""Port tools/circle-dm/services/members-sync.ts (1:1 wg docs/spec/services-sync.md
sekcja 3). Paginacja max 30 stron po 100 (safety cap 3000 czlonkow), skip samego
siebie, can_send_message nadpisywane na true przy kazdym upsercie (quirk 1:1)."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.modules.circle_dm.circle.client import CircleApiError, list_members
from app.modules.circle_dm.circle.jwt_manager import get_jwt_for, invalidate_jwt
from app.modules.circle_dm.models import Member

log = create_logger("members-sync")

MAX_PAGES = 30


async def sync_members_for_account(account_id: int) -> int:
    jwt = await get_jwt_for(account_id)

    total_inserted = 0
    page = 1
    while page <= MAX_PAGES:
        try:
            response = await list_members(jwt.access_token, page=page, per_page=100)
        except CircleApiError as err:
            if err.status == 401:
                await invalidate_jwt(account_id)
            raise

        async with async_session_maker() as session:
            for m in response["records"]:
                if m.get("community_member_id") == jwt.community_member_id:
                    continue

                roles_admin = (m.get("roles") or {}).get("admin")
                values = {
                    "account_id": account_id,
                    "circle_community_member_id": m["community_member_id"],
                    "name": m["name"],
                    "email": m.get("email"),
                    "avatar_url": m.get("avatar_url"),
                    "headline": m.get("headline"),
                    "bio": m.get("bio"),
                    "location": m.get("location"),
                    "last_seen_text": m.get("last_seen_text"),
                    "status": m.get("status"),
                    "is_admin": roles_admin if roles_admin is not None else False,
                    "can_send_message": True,
                    "raw_payload": m,
                    "fetched_at": datetime.now(UTC),
                }
                stmt = (
                    pg_insert(Member)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["account_id", "circle_community_member_id"],
                        set_=values,
                    )
                    .returning(Member.id)
                )
                row = (await session.execute(stmt)).first()
                await session.commit()
                # RETURNING zwraca wiersz tez przy UPDATE - licznik liczy
                # wszystkich przetworzonych czlonkow, nie tylko inserty (1:1).
                if row is not None:
                    total_inserted += 1

        if not response.get("has_next_page"):
            break
        page += 1

    log.info(f"Synced {total_inserted} members for account {account_id}")
    return total_inserted


async def get_cached_members_count(account_id: int) -> int:
    async with async_session_maker() as session:
        return (
            await session.execute(
                select(func.count()).select_from(Member).where(Member.account_id == account_id)
            )
        ).scalar_one()


async def ensure_members_cached(account_id: int) -> None:
    count = await get_cached_members_count(account_id)
    if count == 0:
        await sync_members_for_account(account_id)


async def get_member_by_circle_id(
    account_id: int, circle_community_member_id: int
) -> Member | None:
    async with async_session_maker() as session:
        return (
            await session.execute(
                select(Member)
                .where(
                    Member.account_id == account_id,
                    Member.circle_community_member_id == circle_community_member_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
