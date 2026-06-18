"""Routes modulu members. CALY modul montowany pod /api/members ZA
require_auth (main.py, dependencies=[Depends(require_auth)]) - zero
publicznych endpointow.

[members] implementuje listing + akcje na czlonku:
  GET    /                      lista czlonkow (filtry status/source/protected)
  POST   /                      manual provisioning (source=manual)
  POST   /{member_id}/reinvite  ponowny invite do Circle (port admin-reinvite-circle)
  POST   /{member_id}/protect   flaga protected on/off
  DELETE /{member_id}           natychmiastowe usuniecie z Circle + status removed

Triggery workerow JUZ ISTNIEJA - NIE dodawaj ich w tym pliku:
  cleanup / retry zaproszen / reconcile Klarny ->
    POST /api/billing/admin/workers/{membership_cleanup|invite_retry|
    klarna_reconcile}/run (billing/routes/workers.py),
  sync circle_member_id -> POST /api/members/sync-circle-ids
    (members/routes/maintenance.py).
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.email import normalize_email
from app.core.schemas import dump
from app.modules.admin.services.auth import AuthContext, require_auth
from app.modules.members.models import Member
from app.modules.members.schemas import (
    MemberCreateIn,
    MemberListOut,
    MemberOut,
    ProtectIn,
    ProvisionOut,
    ReinviteIn,
)
from app.modules.members.services import provisioning

router = APIRouter()


@router.get("")
async def list_members(
    status: Literal[
        "invited", "active", "paused", "pending_removal", "removed", "invite_failed"
    ]
    | None = None,
    source: Literal["subscription", "one_time", "manual"] | None = None,
    protected: bool | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    stmt = select(Member).order_by(Member.created_at.desc(), Member.id.desc())
    if status is not None:
        stmt = stmt.where(Member.status == status)
    if source is not None:
        stmt = stmt.where(Member.source == source)
    if protected is not None:
        stmt = stmt.where(Member.protected.is_(protected))
    members = (await session.execute(stmt)).scalars().all()
    return dump(MemberListOut(members=[MemberOut.model_validate(m) for m in members]))


@router.post("")
async def create_member(
    body: MemberCreateIn,
    auth: AuthContext = Depends(require_auth),
) -> dict:
    """Manual provisioning. Bez expires_at czlonek NIE podlega cleanupowi
    (source=manual + brak daty = dostep do odwolania)."""
    email = normalize_email(body.email)
    if not email:
        raise HTTPException(status_code=400, detail="email required")

    result = await provisioning.provision(
        email,
        body.name,
        source="manual",
        expires_at=body.expires_at,
        skip_invitation=body.skip_invitation,
    )
    if body.protected:
        await provisioning.set_protected(result.member_id, True, by=auth.email)
    return dump(
        ProvisionOut(
            member_id=result.member_id,
            email=email,
            circle_invited=result.circle_invited,
            circle_member_id=result.circle_member_id,
            already_active=result.already_active,
        )
    )


@router.post("/{member_id}/reinvite")
async def reinvite_member(
    member_id: int,
    body: ReinviteIn | None = None,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require_auth),
) -> dict:
    """Port admin-reinvite-circle per-id. Source czlonka zostaje bez zmian
    (oryginal przy UPDATE tez nie ruszal stripe_source); czlonek bez zywej
    suby w Stripe i tak wypadnie przy nastepnym cleanupie, chyba ze admin
    ustawi protected."""
    member = await session.get(Member, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    skip_invitation = body.skip_invitation if body is not None else True
    result = await provisioning.provision(
        member.email,
        member.name,
        source=member.source,
        expires_at=None,
        skip_invitation=skip_invitation,
    )
    return dump(
        ProvisionOut(
            member_id=result.member_id,
            email=member.email,
            circle_invited=result.circle_invited,
            circle_member_id=result.circle_member_id,
            already_active=result.already_active,
        )
    )


@router.post("/{member_id}/protect")
async def protect_member(
    member_id: int,
    body: ProtectIn,
    auth: AuthContext = Depends(require_auth),
) -> dict:
    member = await provisioning.set_protected(member_id, body.protected, by=auth.email)
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return dump(MemberOut.model_validate(member))


@router.delete("/{member_id}")
async def delete_member(
    member_id: int,
    auth: AuthContext = Depends(require_auth),
) -> dict:
    """Usuniecie z Circle tu i teraz (nie czeka na cleanup)."""
    outcome = await provisioning.remove_member(member_id, reason="admin_delete", by=auth.email)
    if outcome.code == "not_found":
        raise HTTPException(status_code=404, detail="Member not found")
    if outcome.code == "protected":
        raise HTTPException(status_code=409, detail="Member is protected")
    if outcome.code == "circle_error":
        raise HTTPException(status_code=502, detail="Circle API error")
    return {"success": True, "circleRemoved": outcome.circle_removed}
