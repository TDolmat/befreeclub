"""Akcje admina na billingu. Montowane pod /api/billing/admin ZA require_auth
(sesja panelu fazy 1 - koniec z ADMIN_TOKEN i tokenem hardcoded w zrodle).
Kazda akcja pisze billing.audit_log (services/audit.py).

[billing-lifecycle] implementuje lifecycle subskrypcji (sciezki kanoniczne
z port-kontrakt-2.md; aliasy /pause /extend /cancel skasowane w review 2.1):
  POST /subscriptions/pause    (port admin-pause-subscription)
  POST /subscriptions/extend   (port admin-extend-subscription)
  POST /subscriptions/cancel   (NOWE wg PLAN_LANDING)
  GET  /cancellations          (port admin-list-cancellations)
  GET  /legacy-audit           (port admin-stripe-legacy-audit + dokonczone
                                recent_failures z billing.webhook_events)
  POST /payment-method/send-link ("wyslij link zmiany karty" z panelu)
  POST /webhook-events/{id}/reprocess  (review 2.1: reczne ponowienie eventu
                                po bledzie obslugi albo crashu w trakcie -
                                Stripe nie retry'uje, bo dedup zwraca 200)

[admin-api] implementuje panel Subskrypcje (PLAN_LANDING "Admin" pkt 1):
  GET /subscribers          lista: members + zywe Stripe (oba konta, cache
                            60 s) + ostatni webhook event; filtry + paginacja
  GET /subscribers/{email}  karta osoby: timeline + suby na zywo + atrybucja
  GET /problems             nieudane odnowienia + wygasajace karty

Triggery workerow montuje main.py osobno (/workers/{name}/run, [workers]).
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.schemas import dump
from app.core.stripe_client import StripeAccount
from app.modules.admin.services.auth import AuthContext, require_auth
from app.modules.billing.models import WebhookEvent
from app.modules.billing.schemas import (
    AdminCancelIn,
    AdminExtendIn,
    AdminPauseIn,
    PaymentMethodRequestLinkIn,
)
from app.modules.billing.services import admin_subscribers as subscribers_service
from app.modules.billing.services import audit, webhook_handlers
from app.modules.billing.services import lifecycle_admin as lifecycle_service
from app.modules.billing.services import payment_method as payment_method_service

router = APIRouter()

# Statusy subskrypcji Stripe + "none" (osoby bez zadnej suby, np. manual).
SubStatusFilter = Literal[
    "active",
    "trialing",
    "past_due",
    "unpaid",
    "canceled",
    "incomplete",
    "incomplete_expired",
    "paused",
    "none",
]
MemberStatusFilter = Literal[
    "invited", "active", "paused", "pending_removal", "removed", "invite_failed", "none"
]


def _admin_user_id(auth: AuthContext) -> int | None:
    # Dev: DEV_FAKE_AUTH ma id=0 (nie istnieje w admin.users) -> NULL w audycie.
    return auth.auth_account_id or None


@router.post("/subscriptions/pause")
async def pause_subscription(
    body: AdminPauseIn,
    auth: AuthContext = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await lifecycle_service.pause_subscription(
        session,
        email=body.email,
        freeze_days=body.freeze_days,
        remove_from_circle=body.remove_from_circle,
        admin_user_id=_admin_user_id(auth),
        admin_email=auth.email,
    )


@router.post("/subscriptions/extend")
async def extend_subscription(
    body: AdminExtendIn,
    auth: AuthContext = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await lifecycle_service.extend_subscription(
        session,
        subscription_id=body.subscription_id,
        email=body.email,
        account=body.account,
        resumes_at=body.resumes_at,
        trial_end=body.trial_end,
        add_months=body.add_months,
        clear_pause=body.clear_pause,
        admin_user_id=_admin_user_id(auth),
    )


@router.post("/subscriptions/cancel")
async def cancel_subscription(
    body: AdminCancelIn,
    auth: AuthContext = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await lifecycle_service.cancel_subscription(
        session,
        email=body.email,
        at_period_end=body.at_period_end,
        admin_user_id=_admin_user_id(auth),
    )


@router.get("/cancellations")
async def list_cancellations(session: AsyncSession = Depends(get_session)) -> dict:
    return await lifecycle_service.list_cancellations(session)


@router.get("/legacy-audit")
async def legacy_audit(session: AsyncSession = Depends(get_session)) -> dict:
    return await lifecycle_service.legacy_audit(session)


@router.post("/payment-method/send-link")
async def send_payment_method_link(
    body: PaymentMethodRequestLinkIn,
    auth: AuthContext = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Naprawa #2, czesc panelowa: admin wysyla userowi link zmiany karty.
    W odroznieniu od publicznego endpointu zwraca prawdziwy wynik."""
    result = await payment_method_service.request_link(
        email=body.email, raise_on_send_error=True
    )
    await audit.log_action(
        session,
        admin_user_id=_admin_user_id(auth),
        action="send_payment_method_link",
        target_email=body.email,
        payload=result,
    )
    await session.commit()
    return result


@router.post("/webhook-events/{event_row_id}/reprocess")
async def reprocess_webhook_event(
    event_row_id: int,
    auth: AuthContext = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Reczne ponowienie obslugi eventu webhooka (review 2.1).

    Webhook zwraca 200 takze przy bledzie obslugi (processed_at NULL +
    error), a dedup po event_id polyka redelivery Stripe - bez tego
    endpointu np. polkniety charge.refunded nigdy nie anuluje subow.
    Handlery sa idempotentne (grant/fulfill/cancel), wiec drugi przebieg
    jest bezpieczny. Sukces: processed_at ustawione, error wyczyszczony.
    """
    row = await session.get(WebhookEvent, event_row_id)
    if row is None:
        raise HTTPException(404, "Webhook event not found")

    account = StripeAccount(row.stripe_account)
    try:
        await webhook_handlers.process_event(session, account, row.payload)
    except Exception as err:
        await session.rollback()
        row = await session.get(WebhookEvent, event_row_id)
        if row is not None:
            row.error = str(err)[:2000]
            await session.commit()
        raise HTTPException(502, f"Reprocess failed: {err}") from err

    row.processed_at = datetime.now(UTC)
    row.error = None
    await audit.log_action(
        session,
        admin_user_id=_admin_user_id(auth),
        action="reprocess_webhook_event",
        target_email=None,
        payload={"webhook_event_id": event_row_id, "event_id": row.event_id, "type": row.type},
    )
    await session.commit()
    return {"success": True, "eventId": row.event_id, "type": row.type}


# ── [admin-api] panel Subskrypcje ─────────────────────────────────────────────


@router.get("/subscribers")
async def list_subscribers(
    sub_status: SubStatusFilter | None = Query(None, alias="subStatus"),
    plan: str | None = None,
    account: Literal["current", "legacy"] | None = None,
    payment_failed_days: int | None = Query(None, alias="paymentFailedDays", ge=1, le=365),
    member_status: MemberStatusFilter | None = Query(None, alias="memberStatus"),
    source: Literal["subscription", "one_time", "manual"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200, alias="pageSize"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Polaczony widok members + zywe dane Stripe (oba konta, cache 60 s)
    + ostatni webhook event per email. paymentFailedDays = tylko osoby
    z otwartym payment_failed z ostatnich N dni."""
    result = await subscribers_service.list_subscribers(
        session,
        sub_status=sub_status,
        plan=plan,
        account=account,
        payment_failed_days=payment_failed_days,
        member_status=member_status,
        source=source,
        page=page,
        page_size=page_size,
    )
    return dump(result)


@router.get("/subscribers/{email}")
async def subscriber_detail(
    email: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Karta osoby: timeline (webhooki, members.events, audit_log,
    cancellation_reasons), suby z obu kont NA ZYWO, atrybucja ostatniego
    checkoutu (UTM)."""
    result = await subscribers_service.subscriber_detail(session, email)
    return dump(result)


@router.get("/problems")
async def problems(
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Widok "Problemy": nieudane odnowienia do obslugi (payment_failed bez
    pozniejszego invoice.paid) + karty wygasajace przed nastepnym odnowieniem
    (logika legacy-audit, OBA konta)."""
    result = await subscribers_service.problems(session, days=days)
    return dump(result)
