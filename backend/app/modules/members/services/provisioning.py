"""Provisioning/deprovisioning czlonkostwa Circle (members.members + events).

Sygnatury cross-module ZAMROZONE w port-kontrakt-2.md sekcja 4 - uzywaja ich
[billing-checkout], [billing-webhook], [billing-lifecycle], [workers],
[admin-api].

Semantyka (port upsertow circle_members z confirm-subscription /
stripe-webhook / confirm-klarna-checkout / admin-reinvite-circle):
- provision: invite do Circle (3 proby, backoff 1s/2s jak oryginal) + upsert
  po znormalizowanym emailu + wpis members.events. Caly przebieg serializowany
  per email przez pg_advisory_xact_lock (review 2.1: webhook
  checkout.session.completed i browserowy confirm Klarny odpalaja sie w tej
  samej sekundzie - bez locka przegrany dostawal IntegrityError/500 po
  udanej platnosci i mozliwy byl podwojny invite do Circle; oryginal mial
  upsert ON CONFLICT). Nieudany invite NIE rzuca: circle_invited=False,
  status invite_failed (retry-invites ponowi). Czlonek juz active: bez
  ponownego invite (already_active=True); expires_at aktualizowane TYLKO
  w gore (max - jak reconcile-klarna); source NIE jest degradowane
  z one_time na subscription dopoki expires_at jest w przyszlosci (review
  2.1: inaczej cleanup wyrzucilby czlonka w trakcie oplaconego okresu
  Klarny, gdy dolozona suba padnie); event "extended" tylko gdy cos
  faktycznie sie zmienilo (reconcile co godzine nie zasmieca timeline'u).
- set_pause_state: flip statusu active <-> paused (pauza adminowa #16
  + naturalne wznowienie z webhooka invoice.paid).
- schedule_removal: status pending_removal + event; fizyczne usuniecie
  z Circle robi cleanup (run_cleanup). Chronieni (protected) - no-op False.
- remove_member: natychmiastowe usuniecie (akcja admina DELETE /api/members/{id}).
- is_protected: kolumna members.members.protected zamiast hardcoded
  PROTECTED_EMAILS z circle-cleanup (naprawa - migracja danych ustawi flagi).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_maker
from app.core.email import normalize_email
from app.core.logging import create_logger
from app.modules.members.models import Member, MemberEvent
from app.modules.members.services import circle

log = create_logger("members")


@dataclass(frozen=True)
class ProvisionResult:
    member_id: int
    circle_invited: bool  # czy invite do Circle sie powiodl
    circle_member_id: str | None
    already_active: bool  # czlonek juz byl aktywny (idempotentny re-call)


@dataclass(frozen=True)
class RemoveOutcome:
    ok: bool
    code: str  # "removed" | "not_found" | "protected" | "circle_error"
    circle_removed: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """Naiwne datetime traktujemy jako UTC (request bez strefy)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def record_event(
    session: AsyncSession, member_id: int, kind: str, detail: dict[str, Any]
) -> None:
    """Wpis do members.events (timeline czlonka w panelu admina)."""
    session.add(MemberEvent(member_id=member_id, kind=kind, detail=detail))


async def _get_by_email(session: AsyncSession, email: str) -> Member | None:
    stmt = select(Member).where(Member.email == email).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


def _bump_expires(member: Member, expires_at: datetime | None) -> None:
    """expires_at TYLKO w gore (max stare/nowe) - jak reconcile-klarna-checkouts."""
    expires_at = _aware(expires_at)
    if expires_at is None:
        return
    current = _aware(member.expires_at)
    if current is None or expires_at > current:
        member.expires_at = expires_at


async def provision(
    email: str,
    name: str | None,
    *,
    source: str,  # "subscription" | "one_time" | "manual"
    expires_at: datetime | None = None,  # tylko one_time/manual
    skip_invitation: bool = False,  # True = invite bez maila Circle (re-invite admina)
) -> ProvisionResult:
    """Nadaje dostep do Circle i utrwala stan czlonka. Idempotentne po emailu."""
    email = normalize_email(email)
    async with async_session_maker() as session:
        # Serializacja per email (lock trzymany do commit/rollback) - patrz
        # docstring modulu. hashtext: stabilny hash int4 po stronie PG.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:email))"), {"email": email}
        )
        member = await _get_by_email(session, email)

        if member is not None and member.status == "active":
            # Juz aktywny: bez ponownego invite; aktualizacja zrodla/expires_at
            # (Klarna na aktywnym czlonku = przedluzenie, 1:1 stripe-webhook).
            old_expires = member.expires_at
            old_source = member.source
            keep_one_time_source = (
                source == "subscription"
                and member.source == "one_time"
                and (current := _aware(member.expires_at)) is not None
                and current > _now()
            )
            if not keep_one_time_source:
                member.source = source
            if name:
                member.name = name
            _bump_expires(member, expires_at)
            changed = member.expires_at != old_expires or member.source != old_source
            if changed:
                member.updated_at = _now()
                record_event(
                    session,
                    member.id,
                    "extended",
                    {
                        "source": source,
                        "old_expires_at": old_expires.isoformat() if old_expires else None,
                        "expires_at": (
                            member.expires_at.isoformat() if member.expires_at else None
                        ),
                    },
                )
            await session.commit()
            return ProvisionResult(
                member_id=member.id,
                circle_invited=False,
                circle_member_id=member.circle_member_id,
                already_active=True,
            )

        invite = await circle.invite(email, skip_invitation=skip_invitation)
        # 1:1 confirm-subscription: sukces = HTTP ok ORAZ wyciagniete id
        # (inviteSucceeded = circleMemberId !== null).
        invited = invite.ok and invite.circle_member_id is not None

        if member is None:
            member = Member(email=email)
            session.add(member)
        member.name = name or member.name
        member.source = source
        member.circle_member_id = invite.circle_member_id
        member.status = "active" if invited else "invite_failed"
        _bump_expires(member, expires_at)
        member.updated_at = _now()
        await session.flush()  # id dla nowego wiersza

        record_event(
            session,
            member.id,
            "invited" if invited else "invite_failed",
            {
                "source": source,
                "skip_invitation": skip_invitation,
                "circle_member_id": invite.circle_member_id,
                "expires_at": member.expires_at.isoformat() if member.expires_at else None,
                "detail": invite.detail,
            },
        )
        await session.commit()

        if not invited:
            log.error(
                f"Provisioned {email} but Circle invite FAILED - status invite_failed",
                invite.detail,
            )
        return ProvisionResult(
            member_id=member.id,
            circle_invited=invited,
            circle_member_id=invite.circle_member_id,
            already_active=False,
        )


async def set_pause_state(email: str, paused: bool, *, by: str | None = None) -> bool:
    """Flip statusu czlonka pod pauze adminowa (kontrakt #16).

    paused=True: active -> paused (+event "paused"). paused=False: paused ->
    active (+event "resumed"). Kazdy inny stan = no-op False (np. removed
    po pauzie z remove_from_circle). Wolane przez billing (admin-pause,
    admin-extend/clear_pause, webhook invoice.paid przy wznowieniu).
    """
    email = normalize_email(email)
    async with async_session_maker() as session:
        member = await _get_by_email(session, email)
        if member is None:
            return False
        if paused and member.status == "active":
            member.status = "paused"
            member.updated_at = _now()
            record_event(session, member.id, "paused", {"by": by})
            await session.commit()
            return True
        if not paused and member.status == "paused":
            member.status = "active"
            member.updated_at = _now()
            record_event(session, member.id, "resumed", {"by": by})
            await session.commit()
            return True
        return False


async def schedule_removal(email: str, *, reason: str) -> bool:
    """Oznacza czlonka do usuniecia (pending_removal). False = brak czlonka
    albo protected. Fizyczne usuniecie z Circle robi cleanup."""
    email = normalize_email(email)
    async with async_session_maker() as session:
        member = await _get_by_email(session, email)
        if member is None:
            log.info(f"schedule_removal: no member row for {email}")
            return False
        if member.protected:
            log.info(f"schedule_removal: {email} is PROTECTED, skipping")
            return False
        if member.status == "removed":
            return True  # juz usuniety - cel osiagniety, bez eventu
        member.status = "pending_removal"
        member.updated_at = _now()
        record_event(session, member.id, "removal_scheduled", {"reason": reason})
        await session.commit()
        return True


async def is_protected(email: str) -> bool:
    """Czy konto jest chronione przed deprovisioningiem."""
    email = normalize_email(email)
    async with async_session_maker() as session:
        member = await _get_by_email(session, email)
        return member is not None and member.protected


async def set_protected(member_id: int, value: bool, *, by: str | None = None) -> Member | None:
    """Flaga protected (akcja admina). None = brak czlonka."""
    async with async_session_maker() as session:
        member = await session.get(Member, member_id)
        if member is None:
            return None
        if member.protected != value:
            member.protected = value
            member.updated_at = _now()
            record_event(session, member.id, "protected_changed", {"protected": value, "by": by})
            await session.commit()
        return member


async def remove_member(member_id: int, *, reason: str, by: str | None = None) -> RemoveOutcome:
    """Natychmiastowe usuniecie z Circle + status removed (akcja admina).

    Bez circle_member_id: tylko status removed (1:1 admin-pause: 'jest, ale
    bez ID -> tylko dezaktywacja w DB'). Blad Circle API = stan bez zmian.
    """
    async with async_session_maker() as session:
        member = await session.get(Member, member_id)
        if member is None:
            return RemoveOutcome(ok=False, code="not_found")
        if member.protected:
            return RemoveOutcome(ok=False, code="protected")
        if member.status == "removed":
            return RemoveOutcome(ok=True, code="removed")

        circle_removed = False
        if member.circle_member_id:
            try:
                removed = await circle.remove(member.circle_member_id)
            except circle.CircleConfigError:
                removed = False
            if not removed:
                return RemoveOutcome(ok=False, code="circle_error")
            circle_removed = True

        member.status = "removed"
        member.updated_at = _now()
        record_event(
            session,
            member.id,
            "removed",
            {"reason": reason, "by": by, "circle_removed": circle_removed},
        )
        await session.commit()
        return RemoveOutcome(ok=True, code="removed", circle_removed=circle_removed)
