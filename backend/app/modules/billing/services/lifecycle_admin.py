"""Akcje admina na subskrypcjach (porty admin-* + nowy admin-cancel).

Spec: billing-lifecycle.md sekcja 5. Wszystko za require_auth (sesja panelu
fazy 1 zamiast ADMIN_TOKEN), kazda akcja audytowana do billing.audit_log.

- pause_subscription: port admin-pause-subscription, ale wyszukiwanie subow
  PELNE jak self-service (100 customerow, statusy active/trialing/past_due/
  unpaid) zamiast limit 1 + tylko "active" (prowizorka #10);
  remove_from_circle przez modul members (status, nie bool); po udanej
  pauzie members.status -> "paused" (kontrakt #16, przez
  provisioning.set_pause_state).
- extend_subscription: port admin-extend-subscription 1:1 - mechanizm
  przedluzenia = trial_end, hack zdjecia pauzy pustym stringiem i kolejnosc
  dwustopniowa (Stripe nie pozwala ustawic trial_end na spauzowanej sub);
  gdy koncowy stan suby jest BEZ pauzy, members.status "paused" -> "active"
  (naturalne wznowienie robi tez webhook invoice.paid).
- cancel_subscription: NOWE (PLAN_LANDING: "anuluj natychmiast / koniec
  okresu" z panelu) - brak odpowiednika w edge functions.
- list_cancellations: port admin-list-cancellations (200 wierszy desc).
- legacy_audit: port admin-stripe-legacy-audit; recent_failures DOKONCZONE
  (oryginal zawsze 0) - liczone z billing.webhook_events konta legacy.
"""

import calendar
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.email import normalize_email
from app.core.logging import create_logger, to_iso_string
from app.core.schemas import dump
from app.core.stripe_client import (
    StripeAccount,
    StripeConfigError,
    get_client,
    is_configured,
    search_customers_by_email,
)
from app.modules.billing.models import CancellationReason, WebhookEvent
from app.modules.billing.schemas import CancellationReasonOut
from app.modules.billing.services import audit
from app.modules.billing.services.cancellation import NOT_FOUND_CONFIRM_MESSAGE
from app.modules.billing.services.klarna_grant import add_months_js
from app.modules.billing.services.subscriptions import (
    CANCELLABLE_STATUSES,
    find_subscriptions_by_email,
    obj_get,
    period_end_ts,
)
from app.modules.members.models import Member
from app.modules.members.services import provisioning

log = create_logger("billing.admin")

# Statusy pauzowalne = anulowalne (PAUSABLE_STATUSES samoobslugowego
# pause-subscription, naprawa slabszego algorytmu admin-pause).
PAUSABLE_STATUSES = CANCELLABLE_STATUSES

LEGACY_WEBHOOK_PATH = "/api/billing/webhooks/stripe/legacy"
RECENT_FAILURES_WINDOW_DAYS = 7


def _iso_from_ts(ts: int | float) -> str:
    return to_iso_string(datetime.fromtimestamp(ts, UTC))


# ── admin-pause-subscription -> POST /admin/subscriptions/pause ──────────────


async def _pause_on_account(
    account: StripeAccount, email: str, resumes_at: int
) -> dict[str, Any]:
    """PauseResult 1:1 z oryginalu, ale pelne wyszukiwanie: wszyscy customerzy
    po emailu, pierwsza suba w statusie pauzowalnym."""
    result: dict[str, Any] = {
        "account": account.value,
        "found": False,
        "paused": False,
        "subscriptionId": None,
        "customerId": None,
        "resumesAt": None,
    }
    if not is_configured(account):
        # Oryginal rzucal 500 przy braku ktoregokolwiek klucza; tu konto bez
        # klucza po prostu raportuje sie w wyniku (dev z jednym kontem).
        result["error"] = "Stripe key not configured"
        return result

    client = get_client(account)
    customers = await search_customers_by_email(client, email, limit=100)
    if not customers:
        return result

    result["found"] = True
    result["customerId"] = customers[0].id
    for customer in customers:
        subs = await client.v1.subscriptions.list_async(
            params={"customer": customer.id, "status": "all", "limit": 100}
        )
        sub = next((s for s in subs.data if obj_get(s, "status") in PAUSABLE_STATUSES), None)
        if sub is None:
            continue
        await client.v1.subscriptions.update_async(
            sub.id,
            params={"pause_collection": {"behavior": "void", "resumes_at": resumes_at}},
        )
        log.info(
            f"Paused {account.value} sub {sub.id} for {email}, "
            f"resumes at {_iso_from_ts(resumes_at)}"
        )
        result.update(
            {
                "paused": True,
                "subscriptionId": sub.id,
                "customerId": customer.id,
                "resumesAt": _iso_from_ts(resumes_at),
            }
        )
        return result

    result["error"] = "No active subscription"
    return result


async def _find_member_by_email(session: AsyncSession, email: str) -> Member | None:
    stmt = select(Member).where(Member.email == email).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _remove_from_circle(session: AsyncSession, email: str, admin_email: str | None) -> dict:
    """Usuniecie z Circle przez modul members (status removed, nie bool) -
    ksztalt odpowiedzi {removed, reason} 1:1 z oryginalem."""
    member = await _find_member_by_email(session, email)
    if member is None:
        return {"removed": False, "reason": "Member not found in DB"}
    outcome = await provisioning.remove_member(member.id, reason="admin-pause", by=admin_email)
    if outcome.code == "protected":
        return {"removed": False, "reason": "Member is protected"}
    if outcome.code == "circle_error":
        return {"removed": False, "reason": "Circle API error"}
    if outcome.ok and not outcome.circle_removed:
        return {"removed": False, "reason": "No circle_member_id, marked removed in DB"}
    return {"removed": outcome.circle_removed, "reason": f"Removed ({outcome.code})"}


async def pause_subscription(
    session: AsyncSession,
    *,
    email: str | None,
    freeze_days: int | None,
    remove_from_circle: bool,
    admin_user_id: int | None,
    admin_email: str | None,
) -> dict:
    if not email or not isinstance(email, str):
        raise HTTPException(400, "email required")
    if (
        not isinstance(freeze_days, int)
        or isinstance(freeze_days, bool)
        or not 1 <= freeze_days <= 365
    ):
        raise HTTPException(400, "freeze_days required (1-365)")
    email = normalize_email(email)

    resumes_at = int(time.time()) + freeze_days * 86400
    current_result = await _pause_on_account(StripeAccount.CURRENT, email, resumes_at)
    legacy_result = await _pause_on_account(StripeAccount.LEGACY, email, resumes_at)

    any_paused = current_result["paused"] or legacy_result["paused"]
    any_found = current_result["found"] or legacy_result["found"]

    circle_result: dict | None = None
    if any_paused and remove_from_circle:
        circle_result = await _remove_from_circle(session, email, admin_email)
    elif any_paused:
        # Kontrakt #16: pauza widoczna w members.status (filtr panelu).
        # Przy remove_from_circle status ustawia remove_member (removed).
        await provisioning.set_pause_state(email, True, by=admin_email)

    if any_paused:
        session.add(
            CancellationReason(
                email=email, reason="admin-pause", action="frozen", freeze_days=freeze_days
            )
        )
    await audit.log_action(
        session,
        admin_user_id=admin_user_id,
        action="pause_subscription",
        target_email=email,
        payload={
            "freeze_days": freeze_days,
            "remove_from_circle": remove_from_circle,
            "stripe": {"current": current_result, "legacy": legacy_result},
            "circle": circle_result,
        },
    )
    await session.commit()

    return {
        "success": any_paused,
        "email": email,
        "freeze_days": freeze_days,
        "customer_found": any_found,
        "stripe": {"current": current_result, "legacy": legacy_result},
        "circle": circle_result,
    }


# ── admin-extend-subscription -> POST /admin/subscriptions/extend ────────────


async def extend_subscription(
    session: AsyncSession,
    *,
    subscription_id: str | None,
    email: str | None,
    account: str,
    resumes_at: int | None,
    trial_end: int | None,
    add_months: int | None,
    clear_pause: bool,
    admin_user_id: int | None,
) -> dict:
    """Mechanizm przedluzenia = trial (1:1): trial_end przesuwa nastepne
    obciazenie bez proraty. Hack 1:1: pause_collection="" zdejmuje pauze,
    kolejnosc dwustopniowa bo Stripe nie ustawi trial_end na spauzowanej sub."""
    if account not in ("current", "legacy"):
        raise HTTPException(400, "account must be 'current' or 'legacy'")
    stripe_account = StripeAccount(account)
    try:
        client = get_client(stripe_account)
    except StripeConfigError as err:
        raise HTTPException(500, f"Stripe key for {account} not configured") from err

    if not subscription_id:
        if not email:
            raise HTTPException(400, "subscription_id or email required")
        email = normalize_email(email)
        # Wyszukiwanie 1:1 z oryginalem: 10 customerow, 10 subow, pierwsza
        # suba w stanie innym niz canceled/incomplete_expired. Lookup
        # z fallbackiem search (case-insensitive, review 2.1).
        customers = await search_customers_by_email(client, email, limit=10)
        for customer in customers:
            subs = await client.v1.subscriptions.list_async(
                params={"customer": customer.id, "status": "all", "limit": 10}
            )
            found = next(
                (
                    s
                    for s in subs.data
                    if obj_get(s, "status") not in ("canceled", "incomplete_expired")
                ),
                None,
            )
            if found is not None:
                subscription_id = found.id
                break
        if not subscription_id:
            raise HTTPException(404, f"No active subscription found for {email} on {account}")

    effective_trial_end = trial_end
    if isinstance(add_months, int) and add_months > 0:
        current = await client.v1.subscriptions.retrieve_async(subscription_id)
        items = obj_get(obj_get(current, "items"), "data") or []
        period_end = obj_get(items[0], "current_period_end") if items else None
        if period_end is None:
            period_end = obj_get(current, "trial_end")
        if period_end is None:
            raise HTTPException(400, "Could not determine current_period_end")
        now_s = int(time.time())
        base = period_end if period_end > now_s else now_s
        effective_trial_end = int(
            add_months_js(datetime.fromtimestamp(base, UTC), add_months).timestamp()
        )
        log.info(f"add_months={add_months}: base={base} -> trial_end={effective_trial_end}")

    wants_trial_change = isinstance(effective_trial_end, int)
    wants_pause_change = clear_pause or isinstance(resumes_at, int)

    if wants_trial_change:
        # Krok 1: zdejmij pauze (pusty string - hack 1:1) i ustaw trial_end.
        sub = await client.v1.subscriptions.update_async(
            subscription_id,
            params={
                "proration_behavior": "none",
                "pause_collection": "",
                "trial_end": effective_trial_end,
            },
        )
        log.info(f"Step 1: trial_end set to {effective_trial_end}")
        # Krok 2: ponownie naloz pauze, jesli zazadano.
        if isinstance(resumes_at, int):
            sub = await client.v1.subscriptions.update_async(
                subscription_id,
                params={
                    "proration_behavior": "none",
                    "pause_collection": {"behavior": "void", "resumes_at": resumes_at},
                },
            )
            log.info(f"Step 2: pause re-applied until {resumes_at}")
    elif wants_pause_change:
        params: dict[str, Any] = {"proration_behavior": "none"}
        if clear_pause:
            params["pause_collection"] = ""
        elif isinstance(resumes_at, int):
            params["pause_collection"] = {"behavior": "void", "resumes_at": resumes_at}
        sub = await client.v1.subscriptions.update_async(subscription_id, params=params)
    else:
        raise HTTPException(400, "Nothing to update - provide trial_end, resumes_at, or clear_pause")

    # Kontrakt #16: koncowy stan BEZ pauzy = zdejmij "paused" z members.status
    # (best-effort; czlonka moze nie byc w DB, np. legacy niezmigrowany).
    if not obj_get(sub, "pause_collection"):
        resume_email = email
        if not resume_email:
            customer_ref = obj_get(sub, "customer")
            customer_id = (
                customer_ref if isinstance(customer_ref, str) else obj_get(customer_ref, "id")
            )
            if customer_id:
                try:
                    customer = await client.v1.customers.retrieve_async(customer_id)
                    resume_email = obj_get(customer, "email")
                except Exception as err:
                    log.warn(f"extend: could not resolve customer email for unpause: {err}")
        if resume_email:
            await provisioning.set_pause_state(
                normalize_email(resume_email), False, by="admin-extend"
            )

    await audit.log_action(
        session,
        admin_user_id=admin_user_id,
        action="extend_subscription",
        target_email=email,
        payload={
            "subscription_id": subscription_id,
            "account": account,
            "resumes_at": resumes_at,
            "trial_end": trial_end,
            "add_months": add_months,
            "clear_pause": clear_pause,
            "effective_trial_end": effective_trial_end,
            "status": obj_get(sub, "status"),
        },
    )
    await session.commit()

    pause_collection = obj_get(sub, "pause_collection")
    if pause_collection is not None and hasattr(pause_collection, "to_dict"):
        pause_collection = pause_collection.to_dict()
    sub_trial_end = obj_get(sub, "trial_end")
    items = obj_get(obj_get(sub, "items"), "data") or []
    log.info(f"Updated {account} sub {subscription_id}")
    return {
        "success": True,
        "subscription_id": sub.id,
        "status": obj_get(sub, "status"),
        "pause_collection": pause_collection,
        "trial_end": sub_trial_end,
        "trial_end_iso": _iso_from_ts(sub_trial_end) if sub_trial_end else None,
        "current_period_end": obj_get(items[0], "current_period_end") if items else None,
    }


# ── NOWE: POST /admin/subscriptions/cancel (natychmiast / koniec okresu) ─────


async def cancel_subscription(
    session: AsyncSession,
    *,
    email: str | None,
    at_period_end: bool,
    admin_user_id: int | None,
) -> dict:
    """Anulowanie z panelu admina - bez odpowiednika w edge functions
    (PLAN_LANDING, akcje panelu). Semantyka jak confirm-cancellation
    (cancel_at_period_end na wszystkich pasujacych subach obu kont) albo
    natychmiastowy subscriptions.cancel (jak refundowy handler webhooka)."""
    if not email or not isinstance(email, str):
        raise HTTPException(400, "email required")
    email = normalize_email(email)

    cancelled = 0
    end_date: str | None = None
    for item in await find_subscriptions_by_email(email, statuses=CANCELLABLE_STATUSES):
        client = get_client(item.account)
        sub = item.subscription
        if at_period_end:
            updated = sub
            if not obj_get(sub, "cancel_at_period_end"):
                updated = await client.v1.subscriptions.update_async(
                    sub.id, params={"cancel_at_period_end": True}
                )
            if end_date is None:
                end_ts = period_end_ts(updated, include_cancel_at=True)
                if end_ts is not None:
                    end_date = _iso_from_ts(end_ts)
        else:
            await client.v1.subscriptions.cancel_async(sub.id)
        cancelled += 1
        log.info(
            f"Admin cancel ({'period_end' if at_period_end else 'immediate'}): "
            f"sub {sub.id} ({email}, {item.account.value})"
        )

    if cancelled == 0:
        raise HTTPException(404, NOT_FOUND_CONFIRM_MESSAGE)

    session.add(CancellationReason(email=email, reason="admin-cancel", action="cancelled"))
    await audit.log_action(
        session,
        admin_user_id=admin_user_id,
        action="cancel_subscription",
        target_email=email,
        payload={"at_period_end": at_period_end, "cancelled": cancelled},
    )
    await session.commit()

    return {
        "success": True,
        "cancelled": cancelled,
        "access_until": end_date,
        "mode": "period_end" if at_period_end else "immediate",
    }


# ── admin-list-cancellations -> GET /admin/cancellations ─────────────────────


async def list_cancellations(session: AsyncSession) -> dict:
    """Ostatnie 200 wierszy cancellation_reasons desc, {"rows": [...]} 1:1."""
    stmt = (
        select(CancellationReason).order_by(CancellationReason.created_at.desc()).limit(200)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {"rows": [dump(CancellationReasonOut.model_validate(row)) for row in rows]}


# ── admin-stripe-legacy-audit -> GET /admin/legacy-audit ─────────────────────


def _month_key(ts: int) -> str:
    d = datetime.fromtimestamp(ts, UTC)
    return f"{d.year}-{d.month:02d}"


def _card_expires_before_renewal(
    month: int | None, year: int | None, renewal_ts: int
) -> bool:
    """1:1 isExpiredBeforeRenewal: karta wygasa z koncem miesiaca waznosci."""
    if not month or not year:
        return False
    last_day = calendar.monthrange(year, month)[1]
    card_expiry = datetime(year, month, last_day, 23, 59, 59, tzinfo=UTC)
    return card_expiry < datetime.fromtimestamp(renewal_ts, UTC)


async def _recent_legacy_webhook_failures(session: AsyncSession) -> int:
    """DOKONCZENIE oryginalu (recent_failures zawsze 0): bledy obslugi
    eventow konta legacy z billing.webhook_events z ostatnich 7 dni."""
    since = datetime.now(UTC) - timedelta(days=RECENT_FAILURES_WINDOW_DAYS)
    stmt = (
        select(func.count())
        .select_from(WebhookEvent)
        .where(
            WebhookEvent.stripe_account == "legacy",
            WebhookEvent.error.is_not(None),
            WebhookEvent.created_at >= since,
        )
    )
    return (await session.execute(stmt)).scalar_one()


async def legacy_audit(session: AsyncSession) -> dict:
    """Audyt konta legacy: wszystkie suby w statusach zywych, ryzyka kart
    i metod platnosci, szacowany MRR, stan webhookow. Agregaty 1:1 ze spec."""
    if not is_configured(StripeAccount.LEGACY):
        raise HTTPException(500, "STRIPE_LEGACY_SECRET_KEY not set")
    client = get_client(StripeAccount.LEGACY)

    subs: list[Any] = []
    for status in ("active", "past_due", "unpaid", "trialing", "paused"):
        starting_after: str | None = None
        while True:
            params: dict[str, Any] = {
                "status": status,
                "limit": 100,
                "expand": ["data.default_payment_method", "data.customer"],
            }
            if starting_after:
                params["starting_after"] = starting_after
            page = await client.v1.subscriptions.list_async(params=params)
            subs.extend(page.data)
            if not page.has_more or not page.data:
                break
            starting_after = page.data[-1].id

    rows: list[dict[str, Any]] = []
    for s in subs:
        customer = obj_get(s, "customer")
        customer_email = obj_get(customer, "email") if not isinstance(customer, str) else None
        pm = obj_get(s, "default_payment_method")
        card = obj_get(pm, "card")
        period_end = period_end_ts(s) or 0
        items = obj_get(obj_get(s, "items"), "data") or []
        price = obj_get(items[0], "price") if items else None
        unit_amount = obj_get(price, "unit_amount")
        amount = unit_amount / 100 if unit_amount else None
        if isinstance(amount, float) and amount.is_integer():
            amount = int(amount)
        interval = obj_get(obj_get(price, "recurring"), "interval")
        rows.append(
            {
                "id": s.id,
                "status": obj_get(s, "status"),
                "customer_email": customer_email,
                "current_period_end": period_end,
                "default_pm_id": obj_get(pm, "id"),
                "card_brand": obj_get(card, "brand"),
                "card_last4": obj_get(card, "last4"),
                "card_exp_month": obj_get(card, "exp_month"),
                "card_exp_year": obj_get(card, "exp_year"),
                "card_expires_before_renewal": _card_expires_before_renewal(
                    obj_get(card, "exp_month"), obj_get(card, "exp_year"), period_end
                ),
                "collection_method": obj_get(s, "collection_method"),
                "cancel_at_period_end": obj_get(s, "cancel_at_period_end"),
                "amount_pln": amount,
                "interval": interval,
            }
        )

    by_status: dict[str, int] = {}
    by_renewal_month: dict[str, int] = {}
    no_default_pm: list[dict] = []
    expiring_cards: list[dict] = []
    send_invoice: list[dict] = []
    total_active_mrr = 0.0
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        if r["current_period_end"]:
            mk = _month_key(r["current_period_end"])
            by_renewal_month[mk] = by_renewal_month.get(mk, 0) + 1
        if (
            not r["default_pm_id"]
            and r["collection_method"] == "charge_automatically"
            and r["status"] == "active"
        ):
            no_default_pm.append(r)
        if r["card_expires_before_renewal"] and r["status"] in ("active", "past_due"):
            expiring_cards.append(r)
        if r["collection_method"] == "send_invoice":
            send_invoice.append(r)
        if r["status"] == "active" and r["amount_pln"] and r["interval"]:
            # MRR 1:1 z oryginalem (interwal inny niz month/year = kwartal).
            if r["interval"] == "year":
                monthly = r["amount_pln"] / 12
            elif r["interval"] == "month":
                monthly = r["amount_pln"]
            else:
                monthly = r["amount_pln"] / 3
            total_active_mrr += monthly

    webhook_endpoints: list[dict] = []
    try:
        endpoints = await client.v1.webhook_endpoints.list_async(params={"limit": 20})
        failures = await _recent_legacy_webhook_failures(session)
        # Bledy obslugi znamy tylko dla WLASNEGO endpointu (webhook_events);
        # cudze endpointy raportuja 0 jak oryginal.
        webhook_endpoints = [
            {
                "id": e.id,
                "url": obj_get(e, "url"),
                "status": obj_get(e, "status"),
                "recent_failures": failures
                if LEGACY_WEBHOOK_PATH in (obj_get(e, "url") or "")
                else 0,
            }
            for e in endpoints.data
        ]
    except Exception as err:
        log.warn(f"webhook list failed: {err}")

    return {
        "total_subscriptions": len(rows),
        "by_status": by_status,
        "by_renewal_month": by_renewal_month,
        # Math.round z JS (floor(x+0.5)), nie bankierskie round() Pythona.
        "estimated_mrr_pln": math.floor(total_active_mrr + 0.5),
        "risks": {
            "no_default_payment_method": len(no_default_pm),
            "card_expires_before_renewal": len(expiring_cards),
            "send_invoice_method": len(send_invoice),
        },
        "webhook_endpoints": webhook_endpoints,
        "problem_rows": {
            "no_default_pm": no_default_pm,
            "expiring_cards": expiring_cards,
        },
    }
