"""Panel Subskrypcje ([admin-api]): polaczony widok members + zywe dane Stripe
(oba konta) + historia billing.webhook_events.

Endpointy (routes/admin.py, montowane ZA require_auth):
- GET /api/billing/admin/subscribers          lista z filtrami i paginacja
- GET /api/billing/admin/subscribers/{email}  karta osoby (timeline)
- GET /api/billing/admin/problems             nieudane odnowienia + wygasajace karty

Zrodla danych (PLAN_LANDING "Admin: trzy nowe sekcje" pkt 1):
- Stripe na zywo: snapshot WSZYSTKICH subskrypcji obu kont z cache in-memory
  TTL 60 s (lista nie mieli Stripe przy kazdym odswiezeniu panelu); karta
  osoby pyta Stripe zawsze na zywo (jeden email = tanio),
- billing.webhook_events: "komu cos nie przeszlo i czemu" czytamy z zapisanych
  eventow, nie ze Stripe (fundament panelu wg planu),
- members.members + members.events (odczyt cudzego schematu jest legalny -
  pisze tylko wlasciciel),
- billing.audit_log, billing.cancellation_reasons, billing.checkout_attributions.
"""

import asyncio
import calendar
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.email import normalize_email
from app.core.logging import create_logger, to_iso_string
from app.core.stripe_client import (
    StripeAccount,
    configured_accounts,
    get_client,
    search_customers_by_email,
)
from app.modules.billing.models import (
    AuditLog,
    CancellationReason,
    CheckoutAttribution,
    Plan,
    WebhookEvent,
)
from app.modules.billing.schemas import (
    AdminAttributionOut,
    AdminExpiringCardOut,
    AdminFailedRenewalOut,
    AdminLastEventOut,
    AdminProblemsOut,
    AdminSubscriberDetailOut,
    AdminSubscriberListOut,
    AdminSubscriberRowOut,
    AdminSubscriptionOut,
    AdminTimelineEntryOut,
)
from app.modules.billing.services.subscriptions import obj_get, period_end_ts
from app.modules.members.models import Member, MemberEvent
from app.modules.members.schemas import MemberOut

log = create_logger("billing.admin.subscribers")

SNAPSHOT_TTL_SECONDS = 60
TIMELINE_WEBHOOK_LIMIT = 500
SUBSCRIBER_NOT_FOUND = "Subscriber not found"

# Statusy, dla ktorych wygasajaca karta to realny problem (1:1 z legacy-audit).
_EXPIRING_CARD_STATUSES = ("active", "past_due")
_RESOLVING_EVENT_TYPES = ("invoice.paid", "invoice.payment_succeeded")

_snapshot_cache: dict[StripeAccount, tuple[float, list[Any]]] = {}
_snapshot_lock = asyncio.Lock()


def invalidate_snapshot_cache() -> None:
    """Czysci cache snapshotu Stripe (testy / wymuszenie odswiezenia)."""
    _snapshot_cache.clear()


async def _fetch_account_subscriptions(account: StripeAccount) -> list[Any]:
    """WSZYSTKIE subskrypcje konta (status=all, paginacja po 100) z expandem
    customera (email) i default_payment_method (karta - wygasajace karty)."""
    client = get_client(account)
    subs: list[Any] = []
    starting_after: str | None = None
    while True:
        params: dict[str, Any] = {
            "status": "all",
            "limit": 100,
            "expand": ["data.customer", "data.default_payment_method"],
        }
        if starting_after:
            params["starting_after"] = starting_after
        page = await client.v1.subscriptions.list_async(params=params)
        subs.extend(page.data)
        if not page.has_more or not page.data:
            break
        starting_after = page.data[-1].id
    return subs


async def get_stripe_snapshot() -> dict[StripeAccount, list[Any]]:
    """Snapshot subskrypcji OBU kont z cache in-memory TTL 60 s.

    Lock serializuje odswiezenie - rownolegle requesty panelu nie robia
    stampede na Stripe. Konto bez klucza (dev) po prostu nie wystepuje.
    """
    result: dict[StripeAccount, list[Any]] = {}
    async with _snapshot_lock:
        for account in configured_accounts():
            cached = _snapshot_cache.get(account)
            if cached is None or time.monotonic() - cached[0] >= SNAPSHOT_TTL_SECONDS:
                cached = (time.monotonic(), await _fetch_account_subscriptions(account))
                _snapshot_cache[account] = cached
            result[account] = cached[1]
    return result


# ── budowanie wierszy subskrypcji ─────────────────────────────────────────────


def card_expires_before_renewal(
    month: int | None, year: int | None, renewal_ts: int | None
) -> bool:
    """Logika 1:1 z isExpiredBeforeRenewal (admin-stripe-legacy-audit):
    karta wygasa z koncem miesiaca waznosci."""
    if not month or not year or not renewal_ts:
        return False
    last_day = calendar.monthrange(year, month)[1]
    card_expiry = datetime(year, month, last_day, 23, 59, 59, tzinfo=UTC)
    return card_expiry < datetime.fromtimestamp(renewal_ts, UTC)


def _dt(ts: Any) -> datetime | None:
    if isinstance(ts, int | float) and not isinstance(ts, bool):
        return datetime.fromtimestamp(ts, UTC)
    return None


def _subscription_email(sub: Any) -> str | None:
    """Email z expandowanego customera suba (deleted customer = brak emaila)."""
    customer = obj_get(sub, "customer")
    if customer is None or isinstance(customer, str):
        return None
    if obj_get(customer, "deleted"):
        return None
    email = obj_get(customer, "email")
    return normalize_email(str(email)) if email else None


def _subscription_row(
    account: StripeAccount, sub: Any, price_map: dict[str, str]
) -> AdminSubscriptionOut:
    items = obj_get(obj_get(sub, "items"), "data") or []
    price = obj_get(items[0], "price") if items else None
    price_id = obj_get(price, "id")
    pm = obj_get(sub, "default_payment_method")
    card = obj_get(pm, "card")
    pause = obj_get(sub, "pause_collection")
    period_end = period_end_ts(sub)
    exp_month = obj_get(card, "exp_month")
    exp_year = obj_get(card, "exp_year")
    return AdminSubscriptionOut(
        id=str(obj_get(sub, "id")),
        account=account.value,
        status=str(obj_get(sub, "status") or ""),
        plan_slug=price_map.get(price_id) if price_id else None,
        price_id=price_id,
        amount_pln=obj_get(price, "unit_amount"),
        interval=obj_get(obj_get(price, "recurring"), "interval"),
        current_period_end=_dt(period_end),
        cancel_at_period_end=bool(obj_get(sub, "cancel_at_period_end")),
        pause_resumes_at=_dt(obj_get(pause, "resumes_at")),
        card_brand=obj_get(card, "brand"),
        card_last4=obj_get(card, "last4"),
        card_exp_month=exp_month,
        card_exp_year=exp_year,
        card_expires_before_renewal=card_expires_before_renewal(exp_month, exp_year, period_end),
        created_at=_dt(obj_get(sub, "created")),
    )


async def _price_slug_map(session: AsyncSession) -> dict[str, str]:
    plans = (await session.execute(select(Plan))).scalars().all()
    return {p.stripe_price_id: p.slug for p in plans}


# ── webhook_events: email z payloadu (JSONB) ──────────────────────────────────


def _event_email_expr():
    """Email eventu Stripe z payloadu JSONB - te same pola, z ktorych korzystaja
    handlery webhooka (invoice.customer_email, checkout customer_details.email,
    charge billing_details.email / receipt_email, metadata.email), lower+trim."""
    obj = WebhookEvent.payload["data"]["object"]
    return func.lower(
        func.btrim(
            func.coalesce(
                obj["customer_email"].astext,
                obj["customer_details"]["email"].astext,
                obj["billing_details"]["email"].astext,
                obj["receipt_email"].astext,
                obj["metadata"]["email"].astext,
            )
        )
    )


async def _last_events_by_email(
    session: AsyncSession, emails: list[str]
) -> dict[str, AdminLastEventOut]:
    """Ostatni webhook event per email (DISTINCT ON, tylko emaile z biezacej
    strony listy)."""
    if not emails:
        return {}
    email_expr = _event_email_expr()
    stmt = (
        select(WebhookEvent, email_expr.label("evt_email"))
        .where(email_expr.in_(emails))
        .order_by(email_expr, WebhookEvent.created_at.desc(), WebhookEvent.id.desc())
        .distinct(email_expr)
    )
    out: dict[str, AdminLastEventOut] = {}
    for event, email in (await session.execute(stmt)).all():
        out[email] = AdminLastEventOut(
            type=event.type,
            account=event.stripe_account,
            created_at=event.created_at,
            processed=event.processed_at is not None,
            error=event.error,
        )
    return out


# ── nieudane odnowienia (payment_failed bez pozniejszego invoice.paid) ────────


@dataclass
class OpenFailure:
    email: str | None
    account: str
    subscription_id: str | None
    invoice_id: str | None
    amount_due: int | None
    currency: str | None
    attempt_count: int | None
    next_payment_attempt: datetime | None
    hosted_invoice_url: str | None
    failed_at: datetime
    event_id: str


def _invoice_subscription_id(invoice: dict[str, Any]) -> str | None:
    """OBA pola subscription - stare `invoice.subscription` i nowe
    `invoice.parent.subscription_details.subscription` (naprawa #1)."""

    def _id(value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return value.get("id")
        return None

    sub = _id(invoice.get("subscription"))
    if sub:
        return sub
    details = (invoice.get("parent") or {}).get("subscription_details") or {}
    return _id(details.get("subscription"))


def _event_object(event: WebhookEvent) -> dict[str, Any]:
    payload = event.payload or {}
    return (payload.get("data") or {}).get("object") or {}


def _invoice_email(invoice: dict[str, Any]) -> str | None:
    email = invoice.get("customer_email") or (invoice.get("customer_details") or {}).get("email")
    return normalize_email(str(email)) if email else None


async def open_payment_failures(session: AsyncSession, *, days: int) -> list[OpenFailure]:
    """invoice.payment_failed z ostatnich N dni BEZ pozniejszego invoice.paid /
    invoice.payment_succeeded (match po fakturze, subskrypcji albo emailu).
    Zostaje najnowszy otwarty fail per subskrypcja/email, najswiezsze pierwsze."""
    since = datetime.now(UTC) - timedelta(days=days)
    failures = (
        (
            await session.execute(
                select(WebhookEvent)
                .where(
                    WebhookEvent.type == "invoice.payment_failed",
                    WebhookEvent.created_at >= since,
                )
                .order_by(WebhookEvent.created_at.asc(), WebhookEvent.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not failures:
        return []

    paid_events = (
        (
            await session.execute(
                select(WebhookEvent).where(
                    WebhookEvent.type.in_(_RESOLVING_EVENT_TYPES),
                    WebhookEvent.created_at >= since,
                )
            )
        )
        .scalars()
        .all()
    )
    paid_marks = []
    for event in paid_events:
        invoice = _event_object(event)
        paid_marks.append(
            (
                event.created_at,
                invoice.get("id"),
                _invoice_subscription_id(invoice),
                _invoice_email(invoice),
            )
        )

    open_by_key: dict[str, OpenFailure] = {}
    for event in failures:
        invoice = _event_object(event)
        email = _invoice_email(invoice)
        sub_id = _invoice_subscription_id(invoice)
        invoice_id = invoice.get("id")
        resolved = any(
            paid_at > event.created_at
            and (
                (invoice_id and paid_invoice == invoice_id)
                or (sub_id and paid_sub == sub_id)
                or (email and paid_email == email)
            )
            for paid_at, paid_invoice, paid_sub, paid_email in paid_marks
        )
        key = sub_id or invoice_id or email or event.event_id
        if resolved:
            open_by_key.pop(key, None)
            continue
        open_by_key[key] = OpenFailure(
            email=email,
            account=event.stripe_account,
            subscription_id=sub_id,
            invoice_id=invoice_id,
            amount_due=invoice.get("amount_due"),
            currency=invoice.get("currency"),
            attempt_count=invoice.get("attempt_count"),
            next_payment_attempt=_dt(invoice.get("next_payment_attempt")),
            hosted_invoice_url=invoice.get("hosted_invoice_url"),
            failed_at=event.created_at,
            event_id=event.event_id,
        )
    return sorted(open_by_key.values(), key=lambda f: f.failed_at, reverse=True)


# ── GET /admin/subscribers ────────────────────────────────────────────────────


async def list_subscribers(
    session: AsyncSession,
    *,
    sub_status: str | None = None,
    plan: str | None = None,
    account: str | None = None,
    payment_failed_days: int | None = None,
    member_status: str | None = None,
    source: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> AdminSubscriberListOut:
    snapshot = await get_stripe_snapshot()
    price_map = await _price_slug_map(session)

    subs_by_email: dict[str, list[AdminSubscriptionOut]] = {}
    for acct, subs in snapshot.items():
        for sub in subs:
            email = _subscription_email(sub)
            if not email:
                continue
            subs_by_email.setdefault(email, []).append(_subscription_row(acct, sub, price_map))

    members_by_email: dict[str, Member] = {
        m.email: m for m in (await session.execute(select(Member))).scalars()
    }

    emails = sorted(set(subs_by_email) | set(members_by_email))

    def keep(email: str) -> bool:
        member = members_by_email.get(email)
        subs = subs_by_email.get(email, [])
        if member_status is not None:
            if member_status == "none":
                if member is not None:
                    return False
            elif member is None or member.status != member_status:
                return False
        if source is not None and (member is None or member.source != source):
            return False
        if sub_status is not None:
            if sub_status == "none":
                if subs:
                    return False
            elif not any(s.status == sub_status for s in subs):
                return False
        if plan is not None and not any(s.plan_slug == plan for s in subs):
            return False
        if account is not None and not any(s.account == account for s in subs):
            return False
        return True

    emails = [e for e in emails if keep(e)]

    if payment_failed_days is not None:
        failures = await open_payment_failures(session, days=payment_failed_days)
        failed_emails = {f.email for f in failures if f.email}
        emails = [e for e in emails if e in failed_emails]

    total = len(emails)
    page_emails = emails[(page - 1) * page_size : page * page_size]
    last_events = await _last_events_by_email(session, page_emails)

    rows = [
        AdminSubscriberRowOut(
            email=email,
            member=(
                MemberOut.model_validate(members_by_email[email])
                if email in members_by_email
                else None
            ),
            subscriptions=subs_by_email.get(email, []),
            last_webhook_event=last_events.get(email),
        )
        for email in page_emails
    ]
    return AdminSubscriberListOut(subscribers=rows, total=total, page=page, page_size=page_size)


# ── GET /admin/subscribers/{email} ────────────────────────────────────────────


def _webhook_timeline_detail(event: WebhookEvent) -> dict[str, Any]:
    """Podsumowanie eventu do timeline'u (pelny payload zostaje w DB).
    Dla payment_failed: kwota, numer proby, data nastepnej proby, powod."""
    obj = _event_object(event)
    detail: dict[str, Any] = {
        "eventId": event.event_id,
        "account": event.stripe_account,
        "processed": event.processed_at is not None,
        "error": event.error,
    }
    event_type = event.type
    if event_type == "invoice.payment_failed":
        next_attempt = _dt(obj.get("next_payment_attempt"))
        detail.update(
            {
                "amountDue": obj.get("amount_due"),
                "currency": obj.get("currency"),
                "attemptCount": obj.get("attempt_count"),
                "nextPaymentAttempt": to_iso_string(next_attempt) if next_attempt else None,
                "hostedInvoiceUrl": obj.get("hosted_invoice_url"),
                "failureReason": (obj.get("last_finalization_error") or {}).get("message"),
            }
        )
    elif event_type in _RESOLVING_EVENT_TYPES:
        detail.update(
            {
                "amountPaid": obj.get("amount_paid"),
                "currency": obj.get("currency"),
                "billingReason": obj.get("billing_reason"),
            }
        )
    elif event_type == "charge.refunded":
        detail.update(
            {"amountRefunded": obj.get("amount_refunded"), "currency": obj.get("currency")}
        )
    elif event_type.startswith("checkout.session."):
        detail.update(
            {
                "paymentStatus": obj.get("payment_status"),
                "amountTotal": obj.get("amount_total"),
                "currency": obj.get("currency"),
            }
        )
    elif event_type == "payment_intent.succeeded":
        detail.update(
            {
                "amount": obj.get("amount"),
                "currency": obj.get("currency"),
                "product": (obj.get("metadata") or {}).get("product"),
            }
        )
    return detail


async def _fetch_email_subscriptions(
    email: str, price_map: dict[str, str]
) -> list[AdminSubscriptionOut]:
    """Suby emaila NA ZYWO z obu kont (karta osoby - bez cache), z kartami.
    Lookup z fallbackiem customers.search (case-insensitive, review 2.1)."""
    rows: list[AdminSubscriptionOut] = []
    for account in configured_accounts():
        client = get_client(account)
        customers = await search_customers_by_email(client, email, limit=100)
        for customer in customers:
            subs = await client.v1.subscriptions.list_async(
                params={
                    "customer": customer.id,
                    "status": "all",
                    "limit": 100,
                    "expand": ["data.default_payment_method"],
                }
            )
            for sub in subs.data:
                rows.append(_subscription_row(account, sub, price_map))
    return rows


async def subscriber_detail(session: AsyncSession, email_raw: str) -> AdminSubscriberDetailOut:
    email = normalize_email(email_raw)
    if not email:
        raise HTTPException(status_code=400, detail="email required")

    price_map = await _price_slug_map(session)
    subscriptions = await _fetch_email_subscriptions(email, price_map)

    member = (
        await session.execute(select(Member).where(Member.email == email).limit(1))
    ).scalar_one_or_none()

    timeline: list[AdminTimelineEntryOut] = []

    webhook_events = (
        (
            await session.execute(
                select(WebhookEvent)
                .where(_event_email_expr() == email)
                .order_by(WebhookEvent.created_at.desc(), WebhookEvent.id.desc())
                .limit(TIMELINE_WEBHOOK_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    timeline.extend(
        AdminTimelineEntryOut(
            at=event.created_at,
            source="webhook",
            kind=event.type,
            detail=_webhook_timeline_detail(event),
        )
        for event in webhook_events
    )

    if member is not None:
        member_events = (
            (
                await session.execute(
                    select(MemberEvent)
                    .where(MemberEvent.member_id == member.id)
                    .order_by(MemberEvent.created_at.desc(), MemberEvent.id.desc())
                )
            )
            .scalars()
            .all()
        )
        timeline.extend(
            AdminTimelineEntryOut(
                at=event.created_at, source="member", kind=event.kind, detail=event.detail or {}
            )
            for event in member_events
        )

    audit_rows = (
        (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.target_email == email)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            )
        )
        .scalars()
        .all()
    )
    timeline.extend(
        AdminTimelineEntryOut(
            at=row.created_at,
            source="admin",
            kind=row.action,
            detail={"adminUserId": row.admin_user_id, "payload": row.payload or {}},
        )
        for row in audit_rows
    )

    cancellation_rows = (
        (
            await session.execute(
                select(CancellationReason)
                .where(CancellationReason.email == email)
                .order_by(CancellationReason.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    timeline.extend(
        AdminTimelineEntryOut(
            at=row.created_at,
            source="cancellation",
            kind=row.action,
            detail={"reason": row.reason, "freezeDays": row.freeze_days},
        )
        for row in cancellation_rows
    )

    timeline.sort(key=lambda entry: entry.at, reverse=True)

    attribution_row = (
        await session.execute(
            select(CheckoutAttribution)
            .where(CheckoutAttribution.email == email)
            .order_by(CheckoutAttribution.created_at.desc(), CheckoutAttribution.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    attribution = (
        AdminAttributionOut.model_validate(attribution_row) if attribution_row else None
    )

    if member is None and not subscriptions and not timeline and attribution is None:
        raise HTTPException(status_code=404, detail=SUBSCRIBER_NOT_FOUND)

    return AdminSubscriberDetailOut(
        email=email,
        member=MemberOut.model_validate(member) if member else None,
        subscriptions=subscriptions,
        timeline=timeline,
        attribution=attribution,
    )


# ── GET /admin/problems ───────────────────────────────────────────────────────


async def problems(session: AsyncSession, *, days: int) -> AdminProblemsOut:
    failures = await open_payment_failures(session, days=days)
    failed_renewals = [
        AdminFailedRenewalOut(
            email=f.email,
            account=f.account,
            subscription_id=f.subscription_id,
            invoice_id=f.invoice_id,
            amount_due=f.amount_due,
            currency=f.currency,
            attempt_count=f.attempt_count,
            next_payment_attempt=f.next_payment_attempt,
            hosted_invoice_url=f.hosted_invoice_url,
            failed_at=f.failed_at,
            event_id=f.event_id,
        )
        for f in failures
    ]

    snapshot = await get_stripe_snapshot()
    price_map = await _price_slug_map(session)
    expiring: list[AdminExpiringCardOut] = []
    for account, subs in snapshot.items():
        for sub in subs:
            if obj_get(sub, "status") not in _EXPIRING_CARD_STATUSES:
                continue
            row = _subscription_row(account, sub, price_map)
            if row.card_expires_before_renewal:
                expiring.append(
                    AdminExpiringCardOut(email=_subscription_email(sub), **row.model_dump())
                )

    far_future = datetime.max.replace(tzinfo=UTC)
    expiring.sort(key=lambda r: r.current_period_end or far_future)

    return AdminProblemsOut(failed_renewals=failed_renewals, expiring_cards=expiring)
