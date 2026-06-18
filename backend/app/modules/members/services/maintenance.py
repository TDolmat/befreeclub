"""Narzedzia utrzymaniowe members: retry zaproszen + sync circle_member_id.

Logika jako funkcje. Triggery JUZ ISTNIEJA - NIE dodawaj drugich:
retry zaproszen -> POST /api/billing/admin/workers/invite_retry/run
(billing/routes/workers.py), sync -> POST /api/members/sync-circle-ids
(members/routes/maintenance.py).

retry_failed_invites - port retry-circle-invites z NAPRAWA #6/#7
z PLAN_LANDING: ponawia TYLKO status invite_failed. Oryginal lapal
`active=false OR circle_member_id IS NULL`, czyli re-invitowal takze
celowo wyrzuconych (removed) - koniec z tym, removed jest NIETYKALNE.
Event invite_retried tylko przy SUKCESIE (review 2.1: worker tyka co
godzine - trwale zepsuty invite generowal 24 eventy dziennie na zawsze;
porazka idzie do loggera, pierwotny invite_failed juz jest w events).

sync_circle_ids - port sync-circle-ids 1:1: paginacja Circle per_page=50,
match po lowercase email, uzupelnia brakujace circle_member_id (bez ID
cleanup nie umie nikogo usunac). Filtr stripe_source='legacy' z oryginalu
odpada - nowy enum source nie ma 'legacy'; bierzemy wszystkie zywe wiersze
bez ID.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.modules.members.models import Member
from app.modules.members.services import circle
from app.modules.members.services.provisioning import record_event

log = create_logger("members.maint")

# Statusy z szansa na obecnosc w Circle - tym uzupelniamy brakujace ID.
SYNC_STATUSES = ("invited", "active", "paused", "pending_removal")


@dataclass(frozen=True)
class RetryResult:
    email: str
    success: bool
    circle_member_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class SyncResult:
    circle_total: int
    found: int
    not_found: int
    not_found_emails: list[str] = field(default_factory=list)


async def retry_failed_invites() -> list[RetryResult]:
    """Ponawia zaproszenia TYLKO dla invite_failed (nigdy removed).

    Jak oryginal: jedna proba per czlonek (bez backoffu - to narzedzie
    reczne), z mailem zaproszeniowym (skip_invitation=False), porazka
    jednego nie przerywa reszty.
    """
    results: list[RetryResult] = []
    now = datetime.now(UTC)
    async with async_session_maker() as session:
        members = list(
            (
                await session.execute(
                    select(Member).where(Member.status == "invite_failed").order_by(Member.id)
                )
            )
            .scalars()
            .all()
        )
        log.info(f"Found {len(members)} failed invites")

        for member in members:
            invite = await circle.invite(member.email, skip_invitation=False, retries=1)
            if invite.ok:
                member.circle_member_id = invite.circle_member_id
                member.status = "active"
                member.updated_at = now
                record_event(
                    session,
                    member.id,
                    "invite_retried",
                    {"success": True, "circle_member_id": invite.circle_member_id},
                )
                results.append(
                    RetryResult(
                        email=member.email,
                        success=True,
                        circle_member_id=invite.circle_member_id,
                    )
                )
                log.info(f"Retry success for {member.email} -> {invite.circle_member_id}")
            else:
                # Bez eventu (review 2.1): porazka co godzine = wieczny szum;
                # pierwotny invite_failed jest juz w events, tu tylko log.
                results.append(RetryResult(email=member.email, success=False, error=invite.detail))
                log.warn(f"Retry failed for {member.email}", invite.detail)
            await session.commit()
    return results


async def sync_circle_ids() -> SyncResult:
    """Uzupelnia brakujace circle_member_id matchem po emailu (narzedzie one-off)."""
    mapping = await circle.fetch_all_members()
    log.info(f"Total Circle members: {len(mapping)}")

    found = 0
    not_found_emails: list[str] = []
    async with async_session_maker() as session:
        members = list(
            (
                await session.execute(
                    select(Member)
                    .where(Member.circle_member_id.is_(None), Member.status.in_(SYNC_STATUSES))
                    .order_by(Member.id)
                )
            )
            .scalars()
            .all()
        )
        log.info(f"Members to match: {len(members)}")

        for member in members:
            circle_id = mapping.get(member.email)  # email w DB jest znormalizowany (CHECK)
            if circle_id is None:
                not_found_emails.append(member.email)
                continue
            member.circle_member_id = circle_id
            member.updated_at = datetime.now(UTC)
            record_event(session, member.id, "circle_id_synced", {"circle_member_id": circle_id})
            found += 1
        await session.commit()

    log.info(f"Done. Found: {found}, Not found: {len(not_found_emails)}")
    return SyncResult(
        circle_total=len(mapping),
        found=found,
        not_found=len(not_found_emails),
        not_found_emails=not_found_emails,
    )
