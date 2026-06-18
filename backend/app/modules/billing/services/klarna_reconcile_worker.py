"""Worker reconcile Klarny (port reconcile-klarna-checkouts jako WORKER).

KONIEC z publicznym endpointem (prowizorka #1 speca billing-checkout.md):
sweep odpala asyncio task w lifespan co KLARNA_RECONCILE_INTERVAL_MS
(default 1 h) + reczny trigger admina
POST /api/billing/admin/workers/klarna_reconcile/run (routes/workers.py).

Semantyka sweepa 1:1 z oryginalem: checkout sessions konta CURRENT
z ostatnich 7 dni (paginacja po 100, starting_after), filtr
metadata.source == "klarna_checkout" + payment_status == "paid", email
z customer_email || customer_details.email (normalize), duration
z metadata.duration_months z fallbackiem na billing.plans (zamiast
PLAN_DURATIONS). Nadanie dostepu przez WSPOLNY grant_one_time_access
(idempotentny, expires_at bump TYLKO w gore, mail powitalny raz).

NAPRAWA (PLAN_LANDING bomba #3): sesja z w pelni zrefundowanym charge NIE
nadaje dostepu - grant rzuca PaymentRefundedError, sweep liczy ja jako
skippedRefunded zamiast przywracac dostep (refundowana sesja ma dalej
payment_status=paid).

Wzorzec workera z fazy 1: idempotentny start, guard reentrancy (tick
pomijany, nie kolejkowany), stop = cancel. Reczny trigger (run_now)
serializuje sie z tickiem przez wspolny Lock i zwraca wynik przebiegu.
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.core.email import normalize_email
from app.core.logging import create_logger
from app.core.stripe_client import StripeAccount, get_client, is_configured
from app.modules.billing.services import plans as plans_service
from app.modules.billing.services.checkout import INTERVAL_MONTHS
from app.modules.billing.services.klarna_grant import (
    PaymentRefundedError,
    grant_one_time_access,
)

log = create_logger("workers.reconcile")

# Okno sweepa 1:1 z oryginalem: Klarna potrafi potwierdzac dniami.
LOOKBACK_SECONDS = 7 * 24 * 60 * 60
PAGE_LIMIT = 100

_task: asyncio.Task[None] | None = None
_lock = asyncio.Lock()


@dataclass
class ReconcileSummary:
    scanned: int = 0
    klarna_paid: int = 0
    already_handled: int = 0
    newly_invited: int = 0
    invite_failed: int = 0
    skipped_refunded: int = 0  # NOWY licznik (naprawa refundow)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """Ksztalt odpowiedzi 1:1 z oryginalem + skippedRefunded."""
        return {
            "success": True,
            "scanned": self.scanned,
            "klarnaPaid": self.klarna_paid,
            "alreadyHandled": self.already_handled,
            "newlyInvited": self.newly_invited,
            "inviteFailed": self.invite_failed,
            "skippedRefunded": self.skipped_refunded,
            "errors": self.errors,
        }


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    """Bezpieczny odczyt pola StripeObject (v15 nie jest dict, brak .get())."""
    if obj is None:
        return default
    try:
        return obj[key]
    except (KeyError, TypeError):
        return default


def _obj_id(value: Any) -> str | None:
    """Pole expandowalne Stripe: string id albo obiekt z polem id."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return _obj_get(value, "id")


async def _resolve_duration_months(metadata: Any) -> int:
    """duration_months z metadata; fallback przez billing.plans (1:1
    z fallbackiem PLAN_DURATIONS[plan_id] oryginalu, default semiannual)."""
    try:
        duration = int(_obj_get(metadata, "duration_months") or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration:
        return duration
    plan_slug = _obj_get(metadata, "plan_id") or "semiannual"
    plan = await plans_service.get_by_slug(plan_slug)
    return INTERVAL_MONTHS.get(plan.interval, 0) if plan else 0


async def _handle_session(checkout_session: Any, summary: ReconcileSummary) -> None:
    metadata = _obj_get(checkout_session, "metadata")
    if _obj_get(metadata, "source") != "klarna_checkout":
        return
    if _obj_get(checkout_session, "payment_status") != "paid":
        return
    summary.klarna_paid += 1
    session_id = _obj_get(checkout_session, "id")

    raw_email = _obj_get(checkout_session, "customer_email") or _obj_get(
        _obj_get(checkout_session, "customer_details"), "email"
    )
    email = normalize_email(raw_email or "")
    if not email:
        summary.errors.append(f"Session {session_id}: no email")
        return

    duration_months = await _resolve_duration_months(metadata)
    if not duration_months:
        summary.errors.append(f"Session {session_id}: invalid duration")
        return

    payment_intent_id = _obj_id(_obj_get(checkout_session, "payment_intent"))
    # Kotwica terminu = created sesji: sweep co godzine NIE podbija expires_at
    # (przy "teraz + N" kazdy tick przedluzalby dostep az sesja wypadnie
    # z 7-dniowego okna - review 2.1).
    created_ts = _obj_get(checkout_session, "created")
    purchased_at = datetime.fromtimestamp(int(created_ts), UTC) if created_ts else None
    try:
        result = await grant_one_time_access(
            email=email,
            duration_months=duration_months,
            payment_intent_id=payment_intent_id,
            purchased_at=purchased_at,
        )
    except PaymentRefundedError:
        # Naprawa: refundowana platnosc nie przywraca dostepu.
        summary.skipped_refunded += 1
        log.info(f"Skipped refunded session {session_id} ({email})")
        return
    except Exception as err:
        summary.errors.append(f"Session {session_id}: {err}")
        log.error(f"Grant failed for session {session_id}", str(err))
        return

    if result.already_active:
        summary.already_handled += 1
    elif result.circle_invited:
        summary.newly_invited += 1
        log.info(f"Invited {email} (session {session_id})")
    else:
        # Wiersz invite_failed zostaje - nastepny run / retry-invites ponowi (1:1).
        summary.invite_failed += 1
        log.error(f"Invite FAILED for {email} (session {session_id})")


async def run_reconcile() -> ReconcileSummary:
    """Pojedynczy sweep. Loguje start i wynik (wymog zadania [workers])."""
    log.info("Reconcile run started")
    client = get_client(StripeAccount.CURRENT)
    since = int(time.time()) - LOOKBACK_SECONDS
    summary = ReconcileSummary()

    starting_after: str | None = None
    while True:
        params: dict[str, Any] = {"limit": PAGE_LIMIT, "created": {"gte": since}}
        if starting_after:
            params["starting_after"] = starting_after
        page = await client.v1.checkout.sessions.list_async(params=params)
        data = list(page.data)
        summary.scanned += len(data)
        for checkout_session in data:
            await _handle_session(checkout_session, summary)
        if _obj_get(page, "has_more") and data:
            starting_after = _obj_get(data[-1], "id")
        else:
            break

    log.info("Reconcile run finished", summary.as_dict())
    return summary


async def run_now() -> ReconcileSummary:
    """Reczny trigger (POST /api/billing/admin/workers/klarna_reconcile/run).
    Serializacja z tickiem workera - rownolegly przebieg czeka, nie dubluje."""
    async with _lock:
        return await run_reconcile()


async def _tick() -> None:
    if _lock.locked():
        log.debug("previous run still in progress, skipping tick")
        return
    if not is_configured(StripeAccount.CURRENT):
        log.debug("Stripe current not configured, skipping tick")
        return
    async with _lock:
        await run_reconcile()


async def _loop() -> None:
    while True:
        try:
            await _tick()
        except Exception as err:
            # Blad przebiegu nie moze zabic workera.
            log.error(f"tick failed: {err}")
        await asyncio.sleep(settings.KLARNA_RECONCILE_INTERVAL_MS / 1000)


def start_klarna_reconcile_worker() -> None:
    global _task
    if _task:
        return
    log.info(
        f"Starting klarna_reconcile worker (interval {settings.KLARNA_RECONCILE_INTERVAL_MS}ms)"
    )
    _task = asyncio.create_task(_loop())


def stop_klarna_reconcile_worker() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None
