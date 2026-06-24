"""Logika cleanupu czlonkostw Circle (port circle-cleanup) jako FUNKCJA.

Worker ([workers], services/cleanup_worker.py) i trigger admina
(POST /api/billing/admin/workers/membership_cleanup/run) wolaja
run_cleanup() - tu jest CALA logika. NIE dodawaj drugich triggerow w innych
plikach - zyja w billing/routes/workers.py.

Zmiany vs oryginal (naprawy z PLAN_LANDING + review 2.1):
- iteracja po enum statusu (active/paused/pending_removal) zamiast bool active,
- flaga protected z DB zamiast hardcoded PROTECTED_EMAILS,
- pending_removal (zaplanowane przez schedule_removal, np. refund) usuwane
  bez pytania Stripe,
- source=manual BEZ expires_at pomijany (naprawa quirka "admin dodaje
  recznie, cron wywala"); manual Z expires_at wygasa jak one_time,
- subskrypcyjni przez billing.services.subscriptions.has_live_access
  (OBA konta Stripe, statusy KEEP 1:1 ze spec: active/trialing/past_due/
  unpaid/incomplete/paused + canceled z oplaconym okresem w przyszlosci),
- wygasly one_time/manual NIE jest usuwany, gdy email ma zywa sube w Stripe
  (review 2.1: aktywny subskrybent placacy w torze Klarna dostaje
  source=one_time - po wygasnieciu okna Klarny wylatywalby placac dalej
  za subskrypcje; oryginal dla manual tez pytal Stripe),
- GUARD KONFIGURACJI jak w oryginale (review 2.1, blocker): przebieg
  wymaga OBU kluczy Stripe i konfiguracji Circle, inaczej przerywa PRZED
  przetworzeniem kogokolwiek. Bez guarda deploy bez STRIPE_LEGACY_SECRET_KEY
  wyrzucalby z Circle wszystkich czlonkow z suba tylko na legacy
  (configured_accounts() cicho pomija niekonfigurowane konto),
- events tylko przy zmianie stanu / nieudanej probie usuniecia; czyste
  decyzje keep ida do loggera (review 2.1: ~600 wierszy szumu dziennie
  zatapialo timeline czlonka w panelu).

Quirki zachowane 1:1: one_time bez expires_at zostaje (skip), brak
circle_member_id = brak usuniecia (wiersz czeka na sync-circle-ids),
nieudany DELETE w Circle = retry przy nastepnym przebiegu.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.modules.members.models import Member
from app.modules.members.services import circle
from app.modules.members.services.provisioning import record_event


class CleanupConfigError(RuntimeError):
    """Brak konfiguracji wymaganej do bezpiecznego przebiegu cleanupu."""

log = create_logger("members.cleanup")

# Statusy podlegajace cleanupowi. invited nie wystepuje w runtime (provision
# ustawia od razu active - brak sygnalu akceptacji zaproszenia z Circle),
# removed/invite_failed nie maja czego sprzatac.
CLEANUP_STATUSES = ("active", "paused", "pending_removal")


@dataclass(frozen=True)
class CleanupDecision:
    member_id: int
    email: str
    decision: str
    removed: bool


@dataclass(frozen=True)
class CleanupResult:
    checked: int
    removed: int
    would_remove: int = 0
    dry_run: bool = False
    decisions: list[CleanupDecision] = field(default_factory=list)


async def _has_live_access(email: str) -> bool:
    """Odczyt Stripe przez wspolny serwis billingu (wyjatek od zasady
    'members nie czyta Stripe' - kontrakt sekcja 3). Import lazy, zeby
    members nie zalezal twardo od billingu przy imporcie."""
    from app.modules.billing.services.subscriptions import has_live_access

    return await has_live_access(email)


def _require_config() -> None:
    """1:1 z oryginalem: cleanup NIE rusza bez pelnej konfiguracji.

    has_live_access cicho pomija niekonfigurowane konto Stripe (zwroci False
    zamiast bledu), a decyzja "subscription_dead" jest destrukcyjna - dlatego
    guard PRZED przetworzeniem kogokolwiek, jak throw w circle-cleanup.
    """
    from app.core.stripe_client import StripeAccount, is_configured

    if not is_configured(StripeAccount.CURRENT):
        raise CleanupConfigError("STRIPE_SECRET_KEY not set")
    if not is_configured(StripeAccount.LEGACY):
        raise CleanupConfigError("STRIPE_LEGACY_SECRET_KEY not set")
    if not circle.is_configured():
        raise CleanupConfigError("CIRCLE_API_TOKEN or CIRCLE_COMMUNITY_ID not set")


async def _decide(member: Member, now: datetime) -> tuple[str, bool]:
    """Decyzja per czlonek: (decision, should_remove)."""
    if member.protected:
        return "protected_skip", False
    if member.status == "pending_removal":
        return "pending_removal", True
    if member.source == "manual" and member.expires_at is None:
        # Naprawa quirka #8: reczny dostep bez daty waznosci nie jest
        # weryfikowany w Stripe - zostaje az admin zdecyduje inaczej.
        return "manual_no_expires_skip", False
    if member.source in ("one_time", "manual"):
        if member.expires_at is None:
            # Dlug #17 zachowany 1:1: one_time bez expires_at nigdy nie wygasa.
            return "one_time_no_expires_skip", False
        expires = member.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= now:
            # Review 2.1: zanim usuniemy wygasly dostep czasowy, sprawdz
            # czy email nie ma ZYWEJ suby (subskrybent, ktoremu zakup
            # w torze Klarna nadpisal source na one_time, dalej placi).
            if await _has_live_access(member.email):
                return "one_time_expired_live_sub_keep", False
            return "one_time_expired", True
        return "one_time_active_keep", False
    # Sciezka subskrypcyjna: OBA konta Stripe, usuwamy tylko gdy zadne
    # nie trzyma zywej/oplaconej suby.
    if await _has_live_access(member.email):
        return "subscription_live_keep", False
    return "subscription_dead", True


async def run_cleanup(*, dry_run: bool = False) -> CleanupResult:
    """Przebieg cleanupu: iteracja po active/paused/pending_removal,
    usuniecie z Circle + status removed dla wygaslych/martwych.

    Rzuca CleanupConfigError PRZED przetworzeniem kogokolwiek, gdy brakuje
    ktoregos klucza Stripe albo konfiguracji Circle (jak oryginal) - guard
    obowiazuje takze w trybie cienia (dry_run czyta Stripe przez _decide).

    dry_run=True (TRYB CIENIA): przechodzi CALA logike (sync z DB, _decide
    pytajace Stripe/Circle o stan), loguje KOGO by usunieto i ile zostaje,
    ale NIE woła circle.remove i NIE zmienia statusu czlonka (nikt nie wpada
    w removed, zero eventow). Sluzy do podgladu skutkow przed wlaczeniem
    realnego usuwania. Pole would_remove = ile wierszy zostaloby usunietych."""
    _require_config()
    now = datetime.now(UTC)
    decisions: list[CleanupDecision] = []
    removed_count = 0
    would_remove_count = 0
    mode = "dry_run" if dry_run else "live"

    async with async_session_maker() as session:
        members = list(
            (
                await session.execute(
                    select(Member).where(Member.status.in_(CLEANUP_STATUSES)).order_by(Member.id)
                )
            )
            .scalars()
            .all()
        )
        log.info(f"Checking {len(members)} members (mode={mode})")

        for member in members:
            decision, should_remove = await _decide(member, now)
            removed = False

            if should_remove:
                would_remove_count += 1

            if should_remove and dry_run:
                # TRYB CIENIA: logika przeszla, decyzja "usun" zapadla, ale
                # NIE ruszamy Circle ani statusu - tylko log do podgladu.
                log.info(f"[dry-run] would remove {member.email}: {decision}")
            elif should_remove:
                if not member.circle_member_id:
                    # 1:1 z oryginalem: bez ID nie ma jak usunac - wiersz
                    # zostaje, sync-circle-ids uzupelni ID.
                    decision = f"{decision}:no_circle_member_id"
                else:
                    ok = await circle.remove(member.circle_member_id)
                    if ok:
                        member.status = "removed"
                        member.updated_at = now
                        removed = True
                        removed_count += 1
                    else:
                        # Zostaje - retry przy nastepnym przebiegu (1:1).
                        decision = f"{decision}:remove_failed"

            if removed:
                record_event(
                    session, member.id, "removed", {"by": "cleanup", "decision": decision}
                )
                # Commit per czlonek: czesciowy postep zostaje jak w oryginale
                # (UPDATE leciał od razu po kazdym DELETE).
                await session.commit()
            elif should_remove and not dry_run:
                # Proba usuniecia bez skutku (brak circle_member_id /
                # nieudany DELETE) - to zostaje w events, bo wymaga uwagi.
                # W trybie cienia NIE zapisujemy eventow (zero sladu w DB).
                record_event(session, member.id, "cleanup_decision", {"decision": decision})
                await session.commit()
            # Czyste decyzje keep ida tylko do loggera (review 2.1 - bez
            # ~600 wierszy szumu dziennie w timeline'ie czlonka).

            log.info(f"{member.email}: {decision}")
            decisions.append(
                CleanupDecision(
                    member_id=member.id, email=member.email, decision=decision, removed=removed
                )
            )

    keep_count = len(decisions) - would_remove_count
    log.info(
        f"Done (mode={mode}). Checked {len(decisions)}, keep {keep_count}, "
        f"wouldRemove {would_remove_count}, removed {removed_count}."
    )
    return CleanupResult(
        checked=len(decisions),
        removed=removed_count,
        would_remove=would_remove_count,
        dry_run=dry_run,
        decisions=decisions,
    )
