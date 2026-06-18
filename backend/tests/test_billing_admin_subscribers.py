"""Testy [admin-api]: panel Subskrypcje (GET /admin/subscribers, /problems).

- lista: scalanie members + snapshot Stripe (oba konta) + ostatni webhook
  event per email; wszystkie filtry; paginacja,
- cache snapshotu Stripe (TTL 60 s - lista nie mieli Stripe co request),
- karta osoby: timeline z 4 zrodel (webhook_events, members.events,
  audit_log, cancellation_reasons) + suby na zywo + atrybucja checkoutu,
- problems: payment_failed bez pozniejszego invoice.paid (match po fakturze,
  subie - w tym NOWE pole parent.subscription_details - i emailu) + karty
  wygasajace przed odnowieniem na OBU kontach.

Wymagaja lokalnego Postgresa (DB_HOST/DB_PORT/DB_USER z env) - tworza wlasna
baze `befreeclub_adminapi_test`. Stripe mockowany przez respx.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import stripe_client
from app.core.config import settings
from app.core.db import Base, get_session
from app.main import app
from app.modules.admin.models import User
from app.modules.billing.models import (
    AuditLog,
    CancellationReason,
    CheckoutAttribution,
    Plan,
    WebhookEvent,
)
from app.modules.billing.services import admin_subscribers
from app.modules.members.models import Member, MemberEvent

TEST_DB = "befreeclub_adminapi_test"
STRIPE = "https://api.stripe.com/v1"
CURRENT_H = {"Authorization": "Bearer sk_test_current"}
LEGACY_H = {"Authorization": "Bearer sk_test_legacy"}

NOW = datetime.now(UTC)

TABLES = [
    User.__table__,
    Plan.__table__,
    WebhookEvent.__table__,
    CheckoutAttribution.__table__,
    CancellationReason.__table__,
    AuditLog.__table__,
    Member.__table__,
    MemberEvent.__table__,
]

DDL = [
    "CREATE SCHEMA admin",
    "CREATE SCHEMA billing",
    "CREATE SCHEMA members",
    "CREATE TYPE billing.stripe_account AS ENUM ('current', 'legacy')",
    "CREATE TYPE billing.plan_interval AS ENUM "
    "('month', 'quarter', 'half_year', 'year', 'one_time')",
    "CREATE TYPE billing.attribution_kind AS ENUM ('subscription', 'klarna', 'ebook')",
    "CREATE TYPE billing.cancellation_action AS ENUM ('cancelled', 'frozen')",
    "CREATE TYPE members.member_status AS ENUM ('invited', 'active', 'paused', "
    "'pending_removal', 'removed', 'invite_failed')",
    "CREATE TYPE members.member_source AS ENUM ('subscription', 'one_time', 'manual')",
]

TRUNCATE = (
    "TRUNCATE billing.webhook_events, billing.checkout_attributions, "
    "billing.cancellation_reasons, billing.audit_log, billing.plans, "
    "members.events, members.members RESTART IDENTITY CASCADE"
)


def _dsn(db_name: str) -> str:
    auth = quote(settings.DB_USER, safe="")
    if settings.DB_PASS:
        auth += ":" + quote(settings.DB_PASS, safe="")
    return f"postgresql+asyncpg://{auth}@{settings.DB_HOST}:{settings.DB_PORT}/{db_name}"


async def _create_test_db() -> None:
    admin = create_async_engine(_dsn("postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin.connect() as conn:
        await conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
        await conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    await admin.dispose()

    engine = create_async_engine(_dsn(TEST_DB), poolclass=NullPool)
    async with engine.begin() as conn:
        for ddl in DDL:
            await conn.execute(text(ddl))
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
    await engine.dispose()


@pytest.fixture(scope="module")
def admin_api_db():
    asyncio.run(_create_test_db())
    yield


@pytest.fixture
async def db_maker(admin_api_db):
    engine = create_async_engine(_dsn(TEST_DB), poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(text(TRUNCATE))
    yield maker
    await engine.dispose()


@pytest.fixture
async def api_client(db_maker, monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_current")
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", "sk_test_legacy")
    stripe_client.reset_clients()
    admin_subscribers.invalidate_snapshot_cache()

    async def _override():
        async with db_maker() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
    stripe_client.reset_clients()
    admin_subscribers.invalidate_snapshot_cache()


# ── helpery danych ────────────────────────────────────────────────────────────


def list_json(data):
    return {"object": "list", "data": data, "has_more": False}


def search_json(data):
    return {"object": "search_result", "data": data, "has_more": False,
            "url": "/v1/customers/search"}


def stripe_sub(
    sub_id,
    email,
    *,
    status="active",
    price_id="price_annual",
    unit_amount=120000,
    interval="year",
    period_end=None,
    card=None,
    pause=None,
    cancel_at_period_end=False,
    customer_id="cus_1",
    created=1750000000,
):
    item = {
        "id": f"si_{sub_id}",
        "object": "subscription_item",
        "price": {
            "id": price_id,
            "object": "price",
            "unit_amount": unit_amount,
            "recurring": {"interval": interval},
        },
    }
    if period_end is not None:
        item["current_period_end"] = period_end
    sub = {
        "id": sub_id,
        "object": "subscription",
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "created": created,
        "customer": {"id": customer_id, "object": "customer", "email": email},
        "items": {"object": "list", "data": [item]},
    }
    if card is not None:
        sub["default_payment_method"] = {"id": "pm_1", "object": "payment_method", "card": card}
    if pause is not None:
        sub["pause_collection"] = pause
    return sub


def failed_event(
    event_id,
    email,
    *,
    invoice_id,
    sub_id=None,
    account="current",
    created_at,
    amount_due=26900,
    attempt=1,
    next_attempt_ts=None,
    failure_message=None,
    new_api_field=False,
):
    obj = {
        "id": invoice_id,
        "object": "invoice",
        "customer_email": email,
        "amount_due": amount_due,
        "currency": "pln",
        "attempt_count": attempt,
        "hosted_invoice_url": f"https://invoice.stripe.com/i/{invoice_id}",
    }
    if sub_id:
        if new_api_field:
            obj["parent"] = {"subscription_details": {"subscription": sub_id}}
        else:
            obj["subscription"] = sub_id
    if next_attempt_ts:
        obj["next_payment_attempt"] = next_attempt_ts
    if failure_message:
        obj["last_finalization_error"] = {"message": failure_message}
    return WebhookEvent(
        stripe_account=account,
        event_id=event_id,
        type="invoice.payment_failed",
        payload={"id": event_id, "type": "invoice.payment_failed", "data": {"object": obj}},
        created_at=created_at,
        processed_at=created_at,
    )


def paid_event(
    event_id,
    email,
    *,
    invoice_id,
    sub_id=None,
    account="current",
    created_at,
    event_type="invoice.paid",
    amount_paid=26900,
):
    obj = {
        "id": invoice_id,
        "object": "invoice",
        "customer_email": email,
        "amount_paid": amount_paid,
        "currency": "pln",
        "billing_reason": "subscription_cycle",
    }
    if sub_id:
        obj["subscription"] = sub_id
    return WebhookEvent(
        stripe_account=account,
        event_id=event_id,
        type=event_type,
        payload={"id": event_id, "type": event_type, "data": {"object": obj}},
        created_at=created_at,
        processed_at=created_at,
    )


async def seed_list_data(db_maker):
    """Wspolny seed listy: 2 czlonkow w DB, plan annual, historia webhookow
    (jan: otwarty fail; old: fail rozwiazany pozniejszym invoice.paid)."""
    async with db_maker() as s:
        s.add_all(
            [
                Plan(
                    slug="annual",
                    name="Roczny",
                    stripe_price_id="price_annual",
                    stripe_account="current",
                    amount_pln=120000,
                    interval="year",
                ),
                Member(email="jan@x.pl", name="Jan", status="active", source="subscription"),
                Member(email="kasia@x.pl", name="Kasia", status="removed", source="manual"),
                paid_event(
                    "evt_paid_jan_old",
                    "jan@x.pl",
                    invoice_id="in_0",
                    sub_id="sub_jan",
                    created_at=NOW - timedelta(days=30),
                ),
                failed_event(
                    "evt_fail_jan",
                    "jan@x.pl",
                    invoice_id="in_1",
                    sub_id="sub_jan",
                    created_at=NOW - timedelta(days=2),
                ),
                failed_event(
                    "evt_fail_old",
                    "old@x.pl",
                    invoice_id="in_2",
                    sub_id="sub_old",
                    account="legacy",
                    created_at=NOW - timedelta(days=3),
                ),
                paid_event(
                    "evt_paid_old",
                    "old@x.pl",
                    invoice_id="in_2",
                    sub_id="sub_old",
                    account="legacy",
                    created_at=NOW - timedelta(days=1),
                ),
            ]
        )
        await s.commit()


def mock_snapshot():
    """Snapshot Stripe: jan na current (annual), old na legacy (nieznany plan)."""
    period_end = int((NOW + timedelta(days=90)).timestamp())
    current = respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H).respond(
        200,
        json=list_json(
            [
                stripe_sub(
                    "sub_jan",
                    "Jan@X.pl",  # nieznormalizowany email ze Stripe
                    period_end=period_end,
                    card={"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2031},
                )
            ]
        ),
    )
    legacy = respx.get(f"{STRIPE}/subscriptions", headers=LEGACY_H).respond(
        200,
        json=list_json(
            [
                stripe_sub(
                    "sub_old",
                    "old@x.pl",
                    status="past_due",
                    price_id="price_legacy",
                    unit_amount=26900,
                    interval="month",
                    period_end=period_end,
                    customer_id="cus_old",
                )
            ]
        ),
    )
    return current, legacy


# ── GET /admin/subscribers: scalanie i ksztalt ────────────────────────────────


@respx.mock
async def test_list_subscribers_merges_members_stripe_and_events(api_client, db_maker):
    await seed_list_data(db_maker)
    mock_snapshot()

    res = await api_client.get("/api/billing/admin/subscribers")

    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 3
    assert body["page"] == 1 and body["pageSize"] == 50
    emails = [row["email"] for row in body["subscribers"]]
    assert emails == ["jan@x.pl", "kasia@x.pl", "old@x.pl"]

    jan, kasia, old = body["subscribers"]
    assert jan["member"]["status"] == "active"
    [jan_sub] = jan["subscriptions"]
    assert jan_sub["id"] == "sub_jan"
    assert jan_sub["account"] == "current"
    assert jan_sub["planSlug"] == "annual"
    assert jan_sub["amountPln"] == 120000
    assert jan_sub["cardLast4"] == "4242"
    assert jan_sub["cardExpiresBeforeRenewal"] is False
    assert jan_sub["currentPeriodEnd"].endswith("Z")
    assert jan["lastWebhookEvent"]["type"] == "invoice.payment_failed"
    assert jan["lastWebhookEvent"]["processed"] is True

    assert kasia["member"]["status"] == "removed"
    assert kasia["subscriptions"] == []
    assert kasia["lastWebhookEvent"] is None

    assert old["member"] is None
    [old_sub] = old["subscriptions"]
    assert old_sub["account"] == "legacy"
    assert old_sub["planSlug"] is None  # price spoza billing.plans
    assert old["lastWebhookEvent"]["type"] == "invoice.paid"


@respx.mock
async def test_list_subscribers_filters(api_client, db_maker):
    await seed_list_data(db_maker)
    mock_snapshot()

    async def emails_for(query: str) -> list[str]:
        res = await api_client.get(f"/api/billing/admin/subscribers?{query}")
        assert res.status_code == 200, res.text
        return [row["email"] for row in res.json()["subscribers"]]

    assert await emails_for("subStatus=active") == ["jan@x.pl"]
    assert await emails_for("subStatus=past_due") == ["old@x.pl"]
    assert await emails_for("subStatus=none") == ["kasia@x.pl"]
    assert await emails_for("account=current") == ["jan@x.pl"]
    assert await emails_for("account=legacy") == ["old@x.pl"]
    assert await emails_for("plan=annual") == ["jan@x.pl"]
    assert await emails_for("memberStatus=removed") == ["kasia@x.pl"]
    assert await emails_for("memberStatus=none") == ["old@x.pl"]
    assert await emails_for("source=manual") == ["kasia@x.pl"]
    # jan: otwarty payment_failed; old: fail rozwiazany pozniejszym invoice.paid
    assert await emails_for("paymentFailedDays=7") == ["jan@x.pl"]
    # kombinacja filtrow
    assert await emails_for("subStatus=active&account=legacy") == []


@respx.mock
async def test_list_subscribers_pagination(api_client, db_maker):
    await seed_list_data(db_maker)
    mock_snapshot()

    res = await api_client.get("/api/billing/admin/subscribers?pageSize=1&page=2")

    body = res.json()
    assert body["total"] == 3
    assert body["page"] == 2 and body["pageSize"] == 1
    assert [row["email"] for row in body["subscribers"]] == ["kasia@x.pl"]


@respx.mock
async def test_snapshot_cache_ttl(api_client, db_maker, monkeypatch):
    """Cache in-memory: drugie odswiezenie listy NIE mieli Stripe; po wygasnieciu
    TTL snapshot jest pobierany ponownie."""
    await seed_list_data(db_maker)
    current_route, legacy_route = mock_snapshot()

    await api_client.get("/api/billing/admin/subscribers")
    await api_client.get("/api/billing/admin/subscribers")
    assert current_route.call_count == 1
    assert legacy_route.call_count == 1

    monkeypatch.setattr(admin_subscribers, "SNAPSHOT_TTL_SECONDS", 0)
    await api_client.get("/api/billing/admin/subscribers")
    assert current_route.call_count == 2
    assert legacy_route.call_count == 2


# ── GET /admin/subscribers/{email}: karta osoby ───────────────────────────────


@respx.mock
async def test_subscriber_detail_timeline_and_attribution(api_client, db_maker):
    async with db_maker() as s:
        member = Member(email="jan@x.pl", name="Jan", status="paused", source="subscription")
        s.add(member)
        await s.flush()
        s.add_all(
            [
                Plan(
                    slug="annual",
                    name="Roczny",
                    stripe_price_id="price_annual",
                    stripe_account="current",
                    amount_pln=120000,
                    interval="year",
                ),
                MemberEvent(
                    member_id=member.id,
                    kind="invited",
                    detail={"source": "subscription"},
                    created_at=NOW - timedelta(days=5),
                ),
                MemberEvent(
                    member_id=member.id,
                    kind="paused",
                    detail={},
                    created_at=NOW - timedelta(hours=48),
                ),
                paid_event(
                    "evt_paid",
                    "jan@x.pl",
                    invoice_id="in_0",
                    sub_id="sub_jan",
                    created_at=NOW - timedelta(days=6),
                ),
                failed_event(
                    "evt_fail",
                    "Jan@X.PL ",  # nieznormalizowany email w payloadzie Stripe
                    invoice_id="in_1",
                    sub_id="sub_jan",
                    created_at=NOW - timedelta(hours=47),
                    attempt=2,
                    next_attempt_ts=int((NOW + timedelta(days=3)).timestamp()),
                    failure_message="Your card was declined.",
                ),
                AuditLog(
                    admin_user_id=None,
                    action="pause_subscription",
                    target_email="jan@x.pl",
                    payload={"freeze_days": 14},
                    created_at=NOW - timedelta(hours=36),
                ),
                CancellationReason(
                    email="jan@x.pl",
                    reason="admin-pause",
                    action="frozen",
                    freeze_days=14,
                    created_at=NOW - timedelta(hours=35),
                ),
                CheckoutAttribution(
                    kind="subscription",
                    email="jan@x.pl",
                    stripe_object_id="seti_old",
                    utm_source="google",
                    created_at=NOW - timedelta(days=200),
                ),
                CheckoutAttribution(
                    kind="subscription",
                    email="jan@x.pl",
                    stripe_object_id="seti_new",
                    utm_source="instagram",
                    utm_campaign="czerwiec",
                    fbclid="fb.123",
                    landing_page="https://befreeclub.pl/?utm_source=instagram",
                    created_at=NOW - timedelta(days=3),
                ),
            ]
        )
        await s.commit()

    # Suby NA ZYWO (bez cache): customers po emailu + suby per customer.
    respx.get(f"{STRIPE}/customers", headers=CURRENT_H).respond(
        200, json=list_json([{"id": "cus_1", "object": "customer", "email": "jan@x.pl"}])
    )
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H, params={"customer": "cus_1"}).respond(
        200,
        json=list_json(
            [
                stripe_sub(
                    "sub_jan",
                    "jan@x.pl",
                    status="paused",
                    period_end=int((NOW + timedelta(days=30)).timestamp()),
                    card={"brand": "visa", "last4": "4242", "exp_month": 1, "exp_year": 2027},
                    pause={
                        "behavior": "void",
                        "resumes_at": int((NOW + timedelta(days=14)).timestamp()),
                    },
                )
            ]
        ),
    )
    respx.get(f"{STRIPE}/customers", headers=LEGACY_H).respond(200, json=list_json([]))
    respx.get(f"{STRIPE}/customers/search", headers=LEGACY_H).respond(
        200, json=search_json([])
    )

    # Wielkosc liter w URL nie ma znaczenia (normalize_email).
    res = await api_client.get("/api/billing/admin/subscribers/JAN@X.PL")

    assert res.status_code == 200
    body = res.json()
    assert body["email"] == "jan@x.pl"
    assert body["member"]["status"] == "paused"

    [sub] = body["subscriptions"]
    assert sub["id"] == "sub_jan" and sub["account"] == "current"
    assert sub["planSlug"] == "annual"
    assert sub["cardBrand"] == "visa" and sub["cardLast4"] == "4242"
    assert sub["pauseResumesAt"].endswith("Z")

    # Timeline: 4 zrodla scalone, najnowsze pierwsze.
    entries = [(e["source"], e["kind"]) for e in body["timeline"]]
    assert entries == [
        ("cancellation", "frozen"),
        ("admin", "pause_subscription"),
        ("webhook", "invoice.payment_failed"),
        ("member", "paused"),
        ("member", "invited"),
        ("webhook", "invoice.paid"),
    ]
    failed = body["timeline"][2]["detail"]
    assert failed["amountDue"] == 26900
    assert failed["attemptCount"] == 2
    assert failed["failureReason"] == "Your card was declined."
    assert failed["nextPaymentAttempt"].endswith("Z")
    assert failed["hostedInvoiceUrl"] == "https://invoice.stripe.com/i/in_1"
    admin_entry = body["timeline"][1]["detail"]
    assert admin_entry == {"adminUserId": None, "payload": {"freeze_days": 14}}
    cancellation_entry = body["timeline"][0]["detail"]
    assert cancellation_entry == {"reason": "admin-pause", "freezeDays": 14}

    # Atrybucja OSTATNIEGO checkoutu (nie najstarszego).
    assert body["attribution"]["stripeObjectId"] == "seti_new"
    assert body["attribution"]["utmSource"] == "instagram"
    assert body["attribution"]["utmCampaign"] == "czerwiec"
    assert body["attribution"]["fbclid"] == "fb.123"


@respx.mock
async def test_subscriber_detail_not_found(api_client):
    respx.get(f"{STRIPE}/customers", headers=CURRENT_H).respond(200, json=list_json([]))
    respx.get(f"{STRIPE}/customers", headers=LEGACY_H).respond(200, json=list_json([]))
    respx.get(f"{STRIPE}/customers/search").respond(200, json=search_json([]))

    res = await api_client.get("/api/billing/admin/subscribers/nieznany@x.pl")

    assert res.status_code == 404
    assert res.json() == {"error": "Subscriber not found"}


# ── GET /admin/problems ───────────────────────────────────────────────────────


@respx.mock
async def test_problems_failed_renewals_and_expiring_cards(api_client, db_maker):
    next_attempt = int((NOW + timedelta(days=2)).timestamp())
    async with db_maker() as s:
        s.add_all(
            [
                # Otwarty fail jana - sub w NOWYM polu parent.subscription_details.
                failed_event(
                    "evt_fail_jan",
                    "jan@x.pl",
                    invoice_id="in_10",
                    sub_id="sub_jan",
                    created_at=NOW - timedelta(days=2),
                    amount_due=26900,
                    attempt=2,
                    next_attempt_ts=next_attempt,
                    new_api_field=True,
                ),
                # Fail olda rozwiazany pozniejszym invoice.paid TEJ SAMEJ faktury.
                failed_event(
                    "evt_fail_old",
                    "old@x.pl",
                    invoice_id="in_11",
                    sub_id="sub_old",
                    account="legacy",
                    created_at=NOW - timedelta(days=3),
                ),
                paid_event(
                    "evt_paid_old",
                    "old@x.pl",
                    invoice_id="in_11",
                    account="legacy",
                    created_at=NOW - timedelta(days=1),
                ),
                # Fail uli (bez pola subscription) rozwiazany po EMAILU
                # pozniejszym invoice.payment_succeeded innej faktury.
                failed_event(
                    "evt_fail_ula",
                    "ula@x.pl",
                    invoice_id="in_12",
                    created_at=NOW - timedelta(days=4),
                ),
                paid_event(
                    "evt_paid_ula",
                    "ula@x.pl",
                    invoice_id="in_13",
                    created_at=NOW - timedelta(days=1),
                    event_type="invoice.payment_succeeded",
                ),
                # Fail sprzed 40 dni - poza oknem (default 30 dni).
                failed_event(
                    "evt_fail_ancient",
                    "stary@x.pl",
                    invoice_id="in_14",
                    sub_id="sub_ancient",
                    created_at=NOW - timedelta(days=40),
                ),
            ]
        )
        await s.commit()

    renewal_aug = int(datetime(2026, 8, 1, tzinfo=UTC).timestamp())
    renewal_sep = int(datetime(2026, 9, 15, tzinfo=UTC).timestamp())
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H).respond(
        200,
        json=list_json(
            [
                # Karta wygasa 07/2026, odnowienie 15.09.2026 -> problem.
                stripe_sub(
                    "sub_jan",
                    "jan@x.pl",
                    period_end=renewal_sep,
                    card={"brand": "visa", "last4": "1111", "exp_month": 7, "exp_year": 2026},
                ),
                # Karta wazna do 2031 -> OK.
                stripe_sub(
                    "sub_ok",
                    "ola@x.pl",
                    period_end=renewal_sep,
                    card={"brand": "visa", "last4": "2222", "exp_month": 12, "exp_year": 2031},
                    customer_id="cus_ola",
                ),
            ]
        ),
    )
    respx.get(f"{STRIPE}/subscriptions", headers=LEGACY_H).respond(
        200,
        json=list_json(
            [
                # past_due z karta wygasla przed odnowieniem -> problem (legacy).
                stripe_sub(
                    "sub_leg",
                    "old@x.pl",
                    status="past_due",
                    period_end=renewal_aug,
                    card={"brand": "mastercard", "last4": "3333", "exp_month": 1, "exp_year": 2026},
                    customer_id="cus_old",
                ),
                # canceled nie wchodzi do wygasajacych kart mimo starej karty.
                stripe_sub(
                    "sub_dead",
                    "dead@x.pl",
                    status="canceled",
                    period_end=renewal_aug,
                    card={"brand": "visa", "last4": "4444", "exp_month": 1, "exp_year": 2020},
                    customer_id="cus_dead",
                ),
            ]
        ),
    )

    res = await api_client.get("/api/billing/admin/problems")

    assert res.status_code == 200
    body = res.json()

    [renewal] = body["failedRenewals"]
    assert renewal["email"] == "jan@x.pl"
    assert renewal["account"] == "current"
    assert renewal["subscriptionId"] == "sub_jan"  # z parent.subscription_details
    assert renewal["invoiceId"] == "in_10"
    assert renewal["amountDue"] == 26900
    assert renewal["attemptCount"] == 2
    assert renewal["nextPaymentAttempt"].endswith("Z")
    assert renewal["hostedInvoiceUrl"] == "https://invoice.stripe.com/i/in_10"
    assert renewal["eventId"] == "evt_fail_jan"

    # Oba konta, sortowanie po najblizszym odnowieniu.
    cards = body["expiringCards"]
    assert [(c["id"], c["account"], c["email"]) for c in cards] == [
        ("sub_leg", "legacy", "old@x.pl"),
        ("sub_jan", "current", "jan@x.pl"),
    ]
    assert cards[0]["cardExpiresBeforeRenewal"] is True
    assert cards[0]["cardLast4"] == "3333"
