"""Port tools/circle-dm/routes/members.ts (1:1 wg docs/spec/routes-a.md sekcja 5).
Montowane pod /api/circle-dm/members (za require_auth).

Quirki 1:1: brak TTL cache - ensure_members_cached synchronizuje TYLKO przy
pustym cache (pierwszy GET po wdrozeniu moze trwac); q trafia do ILIKE bez
escapowania %/_; syncedCount = liczba przetworzonych (upsertow), nie nowych.
"""

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.logging import to_iso_string
from app.modules.circle_dm.models import Member, Thread
from app.modules.circle_dm.schemas import MembersSyncBody
from app.modules.circle_dm.services.members_sync import (
    ensure_members_cached,
    sync_members_for_account,
)

router = APIRouter()


def _serialize_member(row: Member) -> dict:
    return {
        "id": row.id,
        "adminAccountId": row.account_id,
        "circleCommunityMemberId": row.circle_community_member_id,
        "name": row.name,
        "email": row.email,
        "avatarUrl": row.avatar_url,
        "headline": row.headline,
        "bio": row.bio,
        "location": row.location,
        "lastSeenText": row.last_seen_text,
        "status": row.status,
        "isAdmin": row.is_admin,
        "canSendMessage": row.can_send_message,
        "fetchedAt": to_iso_string(row.fetched_at),
    }


@router.get("")
async def list_members(
    admin_account_id: int = Query(alias="adminAccountId", gt=0),
    q: str | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
    # '1' -> ukryj czlonkow majacych juz watek na tym koncie (inbox "Pozostali").
    # Picker compose tego nie przekazuje.
    exclude_with_thread: str | None = Query(None, alias="excludeWithThread"),
    db: AsyncSession = Depends(get_session),
) -> dict:
    await ensure_members_cached(admin_account_id)

    conditions = [Member.account_id == admin_account_id]

    if exclude_with_thread == "1":
        conditions.append(
            ~exists(
                select(1).where(
                    Thread.account_id == admin_account_id,
                    Thread.other_participant_id == Member.circle_community_member_id,
                )
            )
        )
    if q and len(q.strip()) > 0:
        like = f"%{q.strip()}%"
        conditions.append(
            or_(Member.name.ilike(like), Member.email.ilike(like), Member.headline.ilike(like))
        )

    rows = (
        (
            await db.execute(
                select(Member)
                .where(*conditions)
                .order_by(func.lower(Member.name).asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return {"members": [_serialize_member(r) for r in rows], "count": len(rows)}


@router.get("/{member_id}")
async def get_member(
    member_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_session),
):
    row = (
        await db.execute(select(Member).where(Member.id == member_id).limit(1))
    ).scalar_one_or_none()
    if row is None:
        return JSONResponse({"error": "member not found"}, status_code=404)
    return _serialize_member(row)


@router.post("/sync")
async def sync_members(payload: MembersSyncBody) -> dict:
    count = await sync_members_for_account(payload.admin_account_id)
    return {"ok": True, "syncedCount": count}
