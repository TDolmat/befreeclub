"""Testy [billing-webhook]: webhooki Stripe obu kont.

Kluczowe scenariusze (zadanie + port-kontrakt-2.md wiersz 8/9):
- zly/brak podpisu -> 400, zero zapisu,
- idempotencja: drugi raz ten sam event_id -> 200 bez akcji,
- invoice.payment_failed z NOWYM ksztaltem pola (parent.subscription_details)
  i starym (invoice.subscription) -> mail 1:1; blad maila zapisany w error,
- charge.refunded ebooka -> TYLKO invalidate tokenow (zero ruszania subow),
- charge.refunded subskrypcji -> cancel na obu kontach + schedule_removal,
- Klarna completed/paid -> grant + CAPI Purchase (event_id = pi),
- payment_intent.succeeded ebook -> fulfillment webhook-first + CAPI,
- invoice.paid subscription_create -> CAPI Purchase (event_id = invoice id),
  odnowienie -> tylko zapis,
- customer.subscription.updated -> tylko zapis,
- endpoint legacy weryfikuje WLASNY sekret.

Wymagaja lokalnego Postgresa (DB_HOST/DB_PORT/DB_USER z env) - tworza wlasna
baze `befreeclub_webhook_test`. Stripe/Resend/Meta mockowane przez respx.
"""

import asyncio
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
import respx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import stripe_client
from app.core.config import settings
from app.core.db import Base, get_session
from app.core.meta_capi import hash_email
from app.main import app
from app.modules.admin.models import User
from app.modules.billing.models import (
    AuditLog,
    CheckoutAttribution,
    EbookDownloadToken,
    EbookOrder,
    Plan,
    WebhookEvent,
)
from app.modules.billing.services import capi_events, klarna_grant
from app.modules.billing.services import ebook as ebook_service
from app.modules.members.services import provisioning
from app.modules.members.services.provisioning import ProvisionResult

TEST_DB = "befreeclub_webhook_test"
RESEND_URL = "https://api.resend.com/emails"
STRIPE = "https://api.stripe.com/v1"
META_URL = "https://graph.facebook.com/v21.0/pixel123/events"

CURRENT_SECRET = "whsec_test_current"
LEGACY_SECRET = "whsec_test_legacy"

CURRENT_PATH = "/api/billing/webhooks/stripe/current"
LEGACY_PATH = "/api/billing/webhooks/stripe/legacy"

TABLES = [
    Plan.__table__,
    CheckoutAttribution.__table__,
    EbookOrder.__table__,
    EbookDownloadToken.__table__,
    WebhookEvent.__table__,
    User.__table__,  # FK audit_log -> admin.users (endpoint reprocess)
    AuditLog.__table__,
]

ENUM_DDL = [
    "CREATE TYPE billing.stripe_account AS ENUM ('current', 'legacy')",
    "CREATE TYPE billing.plan_interval AS ENUM "
    "('month', 'quarter', 'half_year', 'year', 'one_time')",
    "CREATE TYPE billing.attribution_kind AS ENUM ('subscription', 'klarna', 'ebook')",
    "CREATE TYPE billing.ebook_order_status AS ENUM ('pending', 'paid', 'refunded')",
]


def _dsn(dbname: str) -> str:
    auth = quote(settings.DB_USER, safe="")
    if settings.DB_PASS:
        auth += ":" + quote(settings.DB_PASS, safe="")
    return f"postgresql+asyncpg://{auth}@{settings.DB_HOST}:{settings.DB_PORT}/{dbname}"


async def _create_test_db() -> None:
    admin = create_async_engine(_dsn("postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
        await conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    await admin.dispose()

    engine = create_async_engine(_dsn(TEST_DB))
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA billing"))
        await conn.execute(text("CREATE SCHEMA admin"))
        for ddl in ENUM_DDL:
            await conn.execute(text(ddl))
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
    await engine.dispose()


@pytest.fixture(scope="module")
def webhook_db():
    asyncio.run(_create_test_db())
    yield


@pytest.fixture
async def db_maker(webhook_db, monkeypatch):
    engine = create_async_engine(_dsn(TEST_DB))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        await s.execute(
            text(
                "TRUNCATE billing.webhook_events, billing.ebook_download_tokens, "
                "billing.ebook_orders, billing.checkout_attributions, billing.audit_log"
            )
        )
        await s.commit()
    monkeypatch.setattr(ebook_service, "async_session_maker", maker)
    yield maker
    await engine.dispose()


@pytest.fixture
async def api_client(db_maker, monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_current")
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", "sk_test_legacy")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", CURRENT_SECRET)
    monkeypatch.setattr(settings, "STRIPE_LEGACY_WEBHOOK_SECRET", LEGACY_SECRET)
    stripe_client.reset_clients()

    async def _override():
        async with db_maker() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
    stripe_client.reset_clients()


@pytest.fixture
def resend_key(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")


@pytest.fixture
def meta_capi_env(monkeypatch):
    monkeypatch.setattr(settings, "META_PIXEL_ID", "pixel123")
    monkeypatch.setattr(settings, "META_CAPI_TOKEN", "capi_token")


@pytest.fixture
def removal_mock(monkeypatch):
    calls: list[dict] = []

    async def fake_schedule_removal(email, *, reason):
        calls.append({"email": email, "reason": reason})
        return True

    monkeypatch.setattr(provisioning, "schedule_removal", fake_schedule_removal)
    return calls


@pytest.fixture
def grant_mock(monkeypatch):
    state = SimpleNamespace(
        calls=[],
        result=ProvisionResult(
            member_id=1, circle_invited=True, circle_member_id="42", already_active=False
        ),
    )

    async def fake_grant(*, email, duration_months, payment_intent_id, purchased_at=None):
        state.calls.append(
            {
                "email": email,
                "duration_months": duration_months,
                "payment_intent_id": payment_intent_id,
                "purchased_at": purchased_at,
            }
        )
        return state.result

    monkeypatch.setattr(klarna_grant, "grant_one_time_access", fake_grant)
    return state


@pytest.fixture
def pause_state_mock(monkeypatch):
    """provisioning.set_pause_state (flip paused->active z invoice.paid) -
    mock, zeby webhook test nie dotykal schematu members."""
    calls: list[dict] = []

    async def fake_set_pause_state(email, paused, *, by=None):
        calls.append({"email": email, "paused": paused, "by": by})
        return False

    monkeypatch.setattr(provisioning, "set_pause_state", fake_set_pause_state)
    return calls


def sig_headers(body: bytes, secret: str) -> dict[str, str]:
    """Podpis stripe-signature jak Stripe: HMAC-SHA256(secret, '{t}.{payload}')."""
    t = int(time.time())
    mac = hmac.new(secret.encode(), b"%d." % t + body, hashlib.sha256).hexdigest()
    return {"stripe-signature": f"t={t},v1={mac}", "content-type": "application/json"}


def make_event(event_type: str, obj: dict, *, event_id: str = "evt_1", created: int = 1765000000):
    return {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "created": created,
        "data": {"object": obj},
    }


async def post_event(
    client: httpx.AsyncClient,
    event: dict,
    *,
    secret: str = CURRENT_SECRET,
    path: str = CURRENT_PATH,
) -> httpx.Response:
    body = json.dumps(event).encode()
    return await client.post(path, content=body, headers=sig_headers(body, secret))


async def stored_events(maker) -> list[WebhookEvent]:
    async with maker() as s:
        return list((await s.execute(select(WebhookEvent))).scalars().all())


def failed_invoice(
    *,
    new_shape: bool = True,
    attempt_count: int = 1,
    next_payment_attempt: int | None = None,
) -> dict:
    invoice = {
        "id": "in_failed_1",
        "object": "invoice",
        "customer_email": "Jan@X.pl",
        "hosted_invoice_url": "https://invoice.stripe.com/i/in_failed_1",
        "amount_due": 87900,
        "currency": "pln",
        "attempt_count": attempt_count,
        "next_payment_attempt": next_payment_attempt,
    }
    if new_shape:
        # API basil: subskrypcja w parent.subscription_details, starego pola BRAK.
        invoice["parent"] = {"subscription_details": {"subscription": "sub_1", "metadata": {}}}
    else:
        invoice["subscription"] = "sub_1"
    return invoice


# ── podpis ────────────────────────────────────────────────────────────────────


async def test_bad_signature_400(api_client, db_maker):
    body = json.dumps(make_event("invoice.payment_failed", failed_invoice())).encode()
    resp = await api_client.post(
        CURRENT_PATH,
        content=body,
        headers={"stripe-signature": "t=1,v1=deadbeef", "content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "Invalid signature"}
    assert await stored_events(db_maker) == []


async def test_missing_signature_400(api_client, db_maker):
    resp = await api_client.post(CURRENT_PATH, content=b"{}")
    assert resp.status_code == 400
    assert resp.json() == {"error": "Missing stripe-signature"}
    assert await stored_events(db_maker) == []


async def test_wrong_account_secret_400(api_client):
    """Endpoint legacy weryfikuje WLASNY sekret - podpis sekretem current odpada."""
    event = make_event("invoice.payment_failed", failed_invoice())
    resp = await post_event(api_client, event, secret=CURRENT_SECRET, path=LEGACY_PATH)
    assert resp.status_code == 400
    assert resp.json() == {"error": "Invalid signature"}


# ── invoice.payment_failed ────────────────────────────────────────────────────


@respx.mock
async def test_payment_failed_new_field_shape_sends_mail(api_client, db_maker, resend_key):
    """Naprawa #1: faktura w ksztalcie basil (parent.subscription_details,
    BEZ pola invoice.subscription) nie moze byc cichym skipem."""
    mail = respx.post(RESEND_URL).respond(200, json={"id": "m1"})
    next_attempt = int(datetime(2026, 6, 15, 12, 0, tzinfo=UTC).timestamp())
    event = make_event(
        "invoice.payment_failed",
        failed_invoice(new_shape=True, next_payment_attempt=next_attempt),
    )

    resp = await post_event(api_client, event)

    assert resp.status_code == 200
    assert resp.json() == {"received": True}
    assert mail.call_count == 1
    payload = json.loads(mail.calls.last.request.content)
    assert payload["to"] == ["jan@x.pl"]
    assert payload["from"] == "Be Free Club <noreply@befreeclub.pl>"
    assert payload["reply_to"] == "krystian@befreeclub.pl"
    assert payload["subject"] == (
        "⚠️ Płatność za Be Free Club nie powiodła się - autoryzuj jednym kliknięciem"
    )
    html = payload["html"]
    assert "<strong>879.00 PLN</strong>" in html
    assert "https://invoice.stripe.com/i/in_failed_1" in html
    assert "https://befreeclub.pl/aktualizuj-karte" in html
    assert "Następna próba pobrania środków: <strong>15 czerwca 2026</strong>" in html

    [row] = await stored_events(db_maker)
    assert row.stripe_account == "current"
    assert row.event_id == "evt_1"
    assert row.type == "invoice.payment_failed"
    assert row.processed_at is not None
    assert row.error is None
    # Powod/kontekst nieudanej platnosci dla panelu - pelny payload w JSONB.
    assert row.payload["data"]["object"]["amount_due"] == 87900


@respx.mock
async def test_payment_failed_old_field_and_retry_subject(api_client, resend_key):
    mail = respx.post(RESEND_URL).respond(200, json={"id": "m1"})
    event = make_event(
        "invoice.payment_failed", failed_invoice(new_shape=False, attempt_count=3)
    )

    resp = await post_event(api_client, event)

    assert resp.status_code == 200
    payload = json.loads(mail.calls.last.request.content)
    assert payload["subject"] == (
        "⚠️ Ponowna próba 3 - płatność za Be Free Club nie powiodła się"
    )
    # Bez next_payment_attempt blok "Nastepna proba" znika (1:1).
    assert "Następna próba" not in payload["html"]


@respx.mock
async def test_payment_failed_on_legacy_endpoint(api_client, db_maker, resend_key):
    """Naprawa #4: konto legacy dostaje webhook - starzy czlonkowie w koncu
    dostaja mail o nieudanym odnowieniu."""
    mail = respx.post(RESEND_URL).respond(200, json={"id": "m1"})
    event = make_event("invoice.payment_failed", failed_invoice(), event_id="evt_leg_1")

    resp = await post_event(api_client, event, secret=LEGACY_SECRET, path=LEGACY_PATH)

    assert resp.status_code == 200
    assert mail.call_count == 1
    [row] = await stored_events(db_maker)
    assert row.stripe_account == "legacy"
    assert row.processed_at is not None


@respx.mock
async def test_payment_failed_skips_non_subscription_invoice(api_client, db_maker, resend_key):
    mail = respx.post(RESEND_URL).respond(200, json={"id": "m1"})
    invoice = failed_invoice()
    invoice.pop("parent")
    event = make_event("invoice.payment_failed", invoice)

    resp = await post_event(api_client, event)

    assert resp.status_code == 200
    assert not mail.called
    [row] = await stored_events(db_maker)
    assert row.processed_at is not None  # skip to normalna obsluga, nie blad


@respx.mock
async def test_handler_error_recorded_response_still_200(api_client, db_maker, resend_key):
    """Blad obslugi -> wpis error, processed_at NULL, response 200 (kontrakt)."""
    respx.post(RESEND_URL).respond(500, json={"error": "boom"})
    event = make_event("invoice.payment_failed", failed_invoice())

    resp = await post_event(api_client, event)

    assert resp.status_code == 200
    assert resp.json() == {"received": True}
    [row] = await stored_events(db_maker)
    assert row.processed_at is None
    assert row.error is not None
    assert "500" in row.error


# ── idempotencja ──────────────────────────────────────────────────────────────


@respx.mock
async def test_duplicate_event_id_no_second_action(api_client, db_maker, resend_key):
    mail = respx.post(RESEND_URL).respond(200, json={"id": "m1"})
    event = make_event("invoice.payment_failed", failed_invoice(), event_id="evt_dup")

    first = await post_event(api_client, event)
    second = await post_event(api_client, event)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {"received": True}
    assert mail.call_count == 1  # mail NIE poszedl drugi raz
    async with db_maker() as s:
        count = (await s.execute(select(func.count()).select_from(WebhookEvent))).scalar_one()
    assert count == 1


# ── charge.refunded: filtr po produkcie (naprawa #5) ──────────────────────────


async def _seed_ebook_order(maker, *, pi_id: str = "pi_eb_1") -> str:
    async with maker() as s:
        order = EbookOrder(
            email="kupujacy@x.pl",
            stripe_payment_intent_id=pi_id,
            amount_paid=24900,
            status="paid",
        )
        s.add(order)
        await s.flush()
        token = EbookDownloadToken(
            order_id=order.id,
            token="ab" * 32,
            email="kupujacy@x.pl",
            expires_at=datetime(2027, 1, 1, tzinfo=UTC),
        )
        s.add(token)
        await s.commit()
    return token.token


def refunded_charge(
    *,
    metadata: dict | None,
    payment_intent: str | None = "pi_eb_1",
    amount: int = 24900,
    amount_refunded: int | None = None,
    email: str | None = "kupujacy@x.pl",
    customer: str | None = None,
) -> dict:
    return {
        "id": "ch_1",
        "object": "charge",
        "refunded": True,
        "amount": amount,
        "amount_refunded": amount if amount_refunded is None else amount_refunded,
        "payment_intent": payment_intent,
        "metadata": metadata or {},
        "billing_details": {"email": email},
        "receipt_email": None,
        "customer": customer,
    }


@respx.mock
async def test_ebook_refund_only_invalidates_tokens(api_client, db_maker, removal_mock):
    """NAJGRUBSZA MINA oryginalu: refund ebooka NIE rusza subskrypcji ani
    Circle. Zero wywolan Stripe (respx wywali sie na niemockowanym requescie),
    zero schedule_removal - tylko revoke tokenow i status refunded."""
    token = await _seed_ebook_order(db_maker)
    event = make_event(
        "charge.refunded", refunded_charge(metadata={"product": "ebook"})
    )

    resp = await post_event(api_client, event)

    assert resp.status_code == 200
    assert removal_mock == []  # czlonkostwo NIETKNIETE
    async with db_maker() as s:
        order = (await s.execute(select(EbookOrder))).scalar_one()
        token_row = (
            await s.execute(
                select(EbookDownloadToken).where(EbookDownloadToken.token == token)
            )
        ).scalar_one()
    assert order.status == "refunded"
    assert token_row.revoked_at is not None
    [row] = await stored_events(db_maker)
    assert row.processed_at is not None
    assert row.error is None


@respx.mock
async def test_ebook_refund_product_from_pi_metadata(api_client, db_maker, removal_mock):
    """Pas i szelki: gdy charge nie ma metadata.product, filtr doczytuje PI."""
    await _seed_ebook_order(db_maker, pi_id="pi_eb_2")
    respx.get(f"{STRIPE}/payment_intents/pi_eb_2").respond(
        200,
        json={"id": "pi_eb_2", "object": "payment_intent", "metadata": {"product": "ebook"}},
    )
    event = make_event(
        "charge.refunded", refunded_charge(metadata={}, payment_intent="pi_eb_2")
    )

    resp = await post_event(api_client, event)

    assert resp.status_code == 200
    assert removal_mock == []
    async with db_maker() as s:
        order = (await s.execute(select(EbookOrder))).scalar_one()
    assert order.status == "refunded"


@respx.mock
async def test_subscription_refund_cancels_on_both_accounts(api_client, db_maker, removal_mock):
    """Refund subowy 1:1: cancel wszystkich nie-terminalnych subow emaila na
    OBU kontach + schedule_removal (fizyczne usuniecie robi cleanup)."""
    respx.get(f"{STRIPE}/payment_intents/pi_sub_1").respond(
        200, json={"id": "pi_sub_1", "object": "payment_intent", "metadata": {}}
    )
    respx.get(f"{STRIPE}/customers").respond(
        200,
        json={"object": "list", "data": [{"id": "cus_1", "object": "customer"}], "has_more": False},
    )
    respx.get(f"{STRIPE}/subscriptions").respond(
        200,
        json={
            "object": "list",
            "data": [
                {"id": "sub_live", "object": "subscription", "status": "active"},
                {"id": "sub_dead", "object": "subscription", "status": "canceled"},
            ],
            "has_more": False,
        },
    )
    cancel = respx.delete(f"{STRIPE}/subscriptions/sub_live").respond(
        200, json={"id": "sub_live", "object": "subscription", "status": "canceled"}
    )
    event = make_event(
        "charge.refunded",
        refunded_charge(
            metadata={}, payment_intent="pi_sub_1", amount=148900, email="Jan@X.pl"
        ),
    )

    resp = await post_event(api_client, event)

    assert resp.status_code == 200
    assert cancel.call_count == 2  # raz na koncie current, raz na legacy
    assert removal_mock == [{"email": "jan@x.pl", "reason": "refund"}]
    [row] = await stored_events(db_maker)
    assert row.processed_at is not None


@respx.mock
async def test_refund_product_filter_pi_error_fails_closed(api_client, db_maker, removal_mock):
    """REGRESJA (review 2.1, HIGH): blad odczytu PI przy rozstrzyganiu
    produktu NIE moze wpasc w sciezke czlonkowska. Przejsciowy 5xx Stripe
    przy refundzie ebooka bez metadata na chargu -> event error do recznego
    reprocess, ZERO cancelowania subow i removal."""
    respx.get(f"{STRIPE}/payment_intents/pi_eb_err").respond(500, json={"error": "boom"})
    cancel = respx.delete(url__startswith=f"{STRIPE}/subscriptions/").respond(200, json={})
    event = make_event(
        "charge.refunded", refunded_charge(metadata={}, payment_intent="pi_eb_err")
    )

    resp = await post_event(api_client, event)

    assert resp.status_code == 200  # kontrakt: blad obslugi -> mimo to 200
    assert not cancel.called
    assert removal_mock == []
    [row] = await stored_events(db_maker)
    assert row.processed_at is None
    assert row.error is not None


@respx.mock
async def test_ebook_refund_before_fulfillment_creates_tombstone(
    api_client, db_maker, removal_mock
):
    """REGRESJA (review 2.1): refund moze przyjsc PRZED payment_intent.succeeded.
    Tombstone `refunded` blokuje pozniejszy fulfillment (PI po refundzie dalej
    ma status succeeded) - kupujacy po zwrocie nie dostaje linku pobrania."""
    refund_event = make_event(
        "charge.refunded",
        refunded_charge(metadata={"product": "ebook"}, payment_intent="pi_eb_race"),
        event_id="evt_refund_first",
    )
    resp = await post_event(api_client, refund_event)
    assert resp.status_code == 200
    assert removal_mock == []
    async with db_maker() as s:
        order = (
            await s.execute(
                select(EbookOrder).where(EbookOrder.stripe_payment_intent_id == "pi_eb_race")
            )
        ).scalar_one()
    assert order.status == "refunded"  # tombstone

    # Spozniony payment_intent.succeeded: fulfillment ZABLOKOWANY (zero maila).
    pi = {
        "id": "pi_eb_race",
        "object": "payment_intent",
        "status": "succeeded",
        "amount": 24900,
        "currency": "pln",
        "receipt_email": "kupujacy@x.pl",
        "metadata": {"product": "ebook"},
    }
    resp = await post_event(
        api_client, make_event("payment_intent.succeeded", pi, event_id="evt_late_pi")
    )
    assert resp.status_code == 200  # zaden request do Resend (respx by wybuchl)
    async with db_maker() as s:
        order = (
            await s.execute(
                select(EbookOrder).where(EbookOrder.stripe_payment_intent_id == "pi_eb_race")
            )
        ).scalar_one()
        tokens = (await s.execute(select(func.count()).select_from(EbookDownloadToken))).scalar_one()
    assert order.status == "refunded"  # nigdy nie wraca do paid
    assert tokens == 0
    rows = {r.event_id: r for r in await stored_events(db_maker)}
    assert rows["evt_late_pi"].error is not None  # zablokowany fulfillment widoczny w panelu


@respx.mock
async def test_partial_refund_skipped(api_client, db_maker, removal_mock):
    event = make_event(
        "charge.refunded",
        refunded_charge(metadata={}, amount=148900, amount_refunded=10000),
    )

    resp = await post_event(api_client, event)

    assert resp.status_code == 200
    assert removal_mock == []
    [row] = await stored_events(db_maker)
    assert row.processed_at is not None


@respx.mock
async def test_subscription_refund_email_from_event_account(api_client, removal_mock):
    """Bez emaila na chargu: customer doczytany na koncie, z ktorego
    przyszedl event (tu legacy)."""
    legacy_pi = respx.get(f"{STRIPE}/payment_intents/pi_leg_1").respond(
        200, json={"id": "pi_leg_1", "object": "payment_intent", "metadata": {}}
    )
    customer = respx.get(f"{STRIPE}/customers/cus_leg").respond(
        200, json={"id": "cus_leg", "object": "customer", "email": "Stary@Czlonek.pl"}
    )
    respx.get(f"{STRIPE}/customers").respond(
        200, json={"object": "list", "data": [], "has_more": False}
    )
    event = make_event(
        "charge.refunded",
        refunded_charge(
            metadata={}, payment_intent="pi_leg_1", email=None, customer="cus_leg"
        ),
        event_id="evt_leg_ref",
    )

    resp = await post_event(api_client, event, secret=LEGACY_SECRET, path=LEGACY_PATH)

    assert resp.status_code == 200
    assert legacy_pi.called
    assert customer.called
    # Auth doczytania PI/customera = klucz konta legacy.
    assert customer.calls.last.request.headers["authorization"] == "Bearer sk_test_legacy"
    assert removal_mock == [{"email": "stary@czlonek.pl", "reason": "refund"}]


# ── Klarna: checkout.session.completed / async_payment_succeeded ──────────────


KLARNA_CREATED_TS = 1765000000


def klarna_session(
    *, payment_status: str = "paid", session_id: str = "cs_1", pi: str | None = "pi_kl_1"
) -> dict:
    return {
        "id": session_id,
        "object": "checkout.session",
        "payment_status": payment_status,
        "created": KLARNA_CREATED_TS,
        "customer_email": None,
        "customer_details": {"email": "Jan@X.pl"},
        "payment_intent": pi,
        "amount_total": 87900,
        "currency": "pln",
        "metadata": {
            "source": "klarna_checkout",
            "plan_id": "semiannual",
            "duration_months": "6",
            "utm_source": "fb",
        },
    }


@respx.mock
async def test_klarna_completed_grants_and_fires_capi(
    api_client, db_maker, grant_mock, meta_capi_env
):
    async with db_maker() as s:
        s.add(
            CheckoutAttribution(
                kind="klarna",
                stripe_object_id="cs_1",
                utm_source="fb",
                fbp="fb.1.111.222",
                fbc="fb.1.333.IwAR123",
                landing_page="https://befreeclub.pl/?utm_source=fb",
                client_ip="10.0.0.1",
                client_ua="test-ua",
            )
        )
        await s.commit()
    capi = respx.post(META_URL).respond(200, json={"events_received": 1})
    event = make_event("checkout.session.completed", klarna_session(), created=1765432100)

    resp = await post_event(api_client, event)

    assert resp.status_code == 200
    assert grant_mock.calls == [
        {
            "email": "Jan@X.pl",
            "duration_months": 6,
            "payment_intent_id": "pi_kl_1",
            # kotwica terminu = created sesji (review 2.1)
            "purchased_at": datetime.fromtimestamp(KLARNA_CREATED_TS, UTC),
        }
    ]
    assert capi.call_count == 1
    sent = json.loads(capi.calls.last.request.content)
    assert sent["access_token"] == "capi_token"
    [capi_event] = sent["data"]
    assert capi_event["event_name"] == "Purchase"
    assert capi_event["event_id"] == "pi_kl_1"  # event_id = payment_intent (kontrakt 5.2)
    assert capi_event["event_time"] == 1765432100
    assert capi_event["user_data"]["em"] == [hash_email("jan@x.pl")]
    assert capi_event["user_data"]["fbp"] == "fb.1.111.222"
    assert capi_event["user_data"]["fbc"] == "fb.1.333.IwAR123"
    assert capi_event["user_data"]["client_ip_address"] == "10.0.0.1"
    assert capi_event["user_data"]["client_user_agent"] == "test-ua"
    assert capi_event["custom_data"] == {
        "value": 879.0,
        "currency": "pln",
        "content_name": "semiannual",
    }
    assert capi_event["event_source_url"] == "https://befreeclub.pl/?utm_source=fb"


@respx.mock
async def test_klarna_unpaid_waits_for_async_event(api_client, db_maker, grant_mock):
    event = make_event(
        "checkout.session.completed", klarna_session(payment_status="unpaid")
    )

    resp = await post_event(api_client, event)

    assert resp.status_code == 200
    assert grant_mock.calls == []
    [row] = await stored_events(db_maker)
    assert row.processed_at is not None  # skip = obsluzone, czekamy na async event


@respx.mock
async def test_klarna_async_payment_succeeded_grants(api_client, grant_mock, monkeypatch):
    monkeypatch.setattr(settings, "META_PIXEL_ID", None)  # CAPI wylaczone po cichu
    event = make_event("checkout.session.async_payment_succeeded", klarna_session())

    resp = await post_event(api_client, event)

    assert resp.status_code == 200
    assert len(grant_mock.calls) == 1


# ── payment_intent.succeeded (ebook webhook-first) ────────────────────────────


@respx.mock
async def test_ebook_pi_succeeded_fulfills_and_fires_capi(
    api_client, db_maker, resend_key, meta_capi_env
):
    mail = respx.post(RESEND_URL).respond(200, json={"id": "m1"})
    capi = respx.post(META_URL).respond(200, json={"events_received": 1})
    pi = {
        "id": "pi_eb_9",
        "object": "payment_intent",
        "status": "succeeded",
        "amount": 24900,
        "currency": "pln",
        "receipt_email": "Kupujacy@X.pl",
        "metadata": {"product": "ebook"},
    }
    event = make_event("payment_intent.succeeded", pi)

    resp = await post_event(api_client, event)

    assert resp.status_code == 200
    assert mail.call_count == 1  # fulfillment bez udzialu przegladarki
    async with db_maker() as s:
        order = (await s.execute(select(EbookOrder))).scalar_one()
    assert order.status == "paid"
    assert order.email == "kupujacy@x.pl"
    assert order.stripe_payment_intent_id == "pi_eb_9"
    assert order.email_sent_at is not None

    [capi_event] = json.loads(capi.calls.last.request.content)["data"]
    assert capi_event["event_name"] == "Purchase"
    assert capi_event["event_id"] == "pi_eb_9"
    assert capi_event["custom_data"] == {
        "value": 249.0,
        "currency": "pln",
        "content_name": "ebook",
    }
    assert capi_event["user_data"]["em"] == [hash_email("kupujacy@x.pl")]

    [row] = await stored_events(db_maker)
    assert row.processed_at is not None


@respx.mock
async def test_non_ebook_pi_succeeded_stored_only(api_client, db_maker, removal_mock):
    pi = {"id": "pi_other", "object": "payment_intent", "status": "succeeded", "metadata": {}}
    event = make_event("payment_intent.succeeded", pi)

    resp = await post_event(api_client, event)

    assert resp.status_code == 200
    async with db_maker() as s:
        orders = (await s.execute(select(func.count()).select_from(EbookOrder))).scalar_one()
    assert orders == 0
    [row] = await stored_events(db_maker)
    assert row.processed_at is not None


@respx.mock
async def test_ebook_pi_without_receipt_email_falls_back_to_charge(
    api_client, db_maker, resend_key
):
    """Hardening (review 2.1): PI bez receipt_email (front nie przekazal) -
    email z billing_details latest_charge, fulfillment nie przepada."""
    charge_route = respx.get(f"{STRIPE}/charges/ch_fb_1").respond(
        200,
        json={
            "id": "ch_fb_1",
            "object": "charge",
            "billing_details": {"email": "Fallback@X.pl"},
        },
    )
    mail = respx.post(RESEND_URL).respond(200, json={"id": "m1"})
    pi = {
        "id": "pi_eb_noemail",
        "object": "payment_intent",
        "status": "succeeded",
        "amount": 24900,
        "currency": "pln",
        "receipt_email": None,
        "latest_charge": "ch_fb_1",
        "metadata": {"product": "ebook"},
    }

    resp = await post_event(api_client, make_event("payment_intent.succeeded", pi))

    assert resp.status_code == 200
    assert charge_route.called
    assert mail.call_count == 1
    async with db_maker() as s:
        order = (await s.execute(select(EbookOrder))).scalar_one()
    assert order.email == "fallback@x.pl"
    assert order.status == "paid"
    [row] = await stored_events(db_maker)
    assert row.processed_at is not None
    assert row.error is None


# ── POST /admin/webhook-events/{id}/reprocess (review 2.1) ───────────────────


@respx.mock
async def test_reprocess_failed_event(api_client, db_maker, resend_key, monkeypatch):
    """Polkniety event (error, processed_at NULL; Stripe nie retry'uje przez
    dedup) da sie recznie ponowic z panelu - sukces czysci error."""
    monkeypatch.setattr(settings, "NODE_ENV", "development")  # require_auth -> dev
    mail = respx.post(RESEND_URL)
    mail.side_effect = [
        httpx.Response(500, json={"error": "boom"}),
        httpx.Response(200, json={"id": "m1"}),
    ]
    event = make_event("invoice.payment_failed", failed_invoice(), event_id="evt_repro")

    resp = await post_event(api_client, event)
    assert resp.status_code == 200
    [row] = await stored_events(db_maker)
    assert row.error is not None and row.processed_at is None

    resp = await api_client.post(f"/api/billing/admin/webhook-events/{row.id}/reprocess")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["eventId"] == "evt_repro"
    assert mail.call_count == 2  # drugi przebieg faktycznie wyslal mail
    [row] = await stored_events(db_maker)
    assert row.processed_at is not None
    assert row.error is None


async def test_reprocess_unknown_event_404(api_client, monkeypatch):
    monkeypatch.setattr(settings, "NODE_ENV", "development")
    resp = await api_client.post("/api/billing/admin/webhook-events/999999/reprocess")
    assert resp.status_code == 404


# ── invoice.paid / payment_succeeded: CAPI Purchase pierwszej faktury suba ────


def paid_invoice(*, billing_reason: str = "subscription_create") -> dict:
    return {
        "id": "in_paid_1",
        "object": "invoice",
        "billing_reason": billing_reason,
        "customer_email": "Jan@X.pl",
        "amount_paid": 148900,
        "currency": "pln",
        "parent": {
            "subscription_details": {
                "subscription": "sub_1",
                "metadata": {"plan_id": "annual", "fbp": "fb.1.555.666"},
            }
        },
    }


@respx.mock
async def test_invoice_paid_first_invoice_fires_capi(
    api_client, db_maker, meta_capi_env, pause_state_mock
):
    async with db_maker() as s:
        s.add(
            CheckoutAttribution(
                kind="subscription",
                stripe_object_id="seti_1",
                email="jan@x.pl",
                fbclid="IwAR999",
                landing_page="https://befreeclub.pl/?fbclid=IwAR999",
                client_ip="10.0.0.2",
                client_ua="sub-ua",
            )
        )
        await s.commit()
    async with db_maker() as s:
        attr = (await s.execute(select(CheckoutAttribution))).scalar_one()
        expected_fbc = f"fb.1.{int(attr.created_at.timestamp() * 1000)}.IwAR999"
    capi = respx.post(META_URL).respond(200, json={"events_received": 1})
    event = make_event("invoice.paid", paid_invoice(), created=1765432999)

    resp = await post_event(api_client, event)

    assert resp.status_code == 200
    [capi_event] = json.loads(capi.calls.last.request.content)["data"]
    assert capi_event["event_name"] == "Purchase"
    assert capi_event["event_id"] == "in_paid_1"  # event_id = id pierwszej faktury
    assert capi_event["event_time"] == 1765432999
    assert capi_event["user_data"]["em"] == [hash_email("jan@x.pl")]
    # fbc zbudowany z fbclid atrybucji: fb.1.<created_at_ms>.<fbclid>
    assert capi_event["user_data"]["fbc"] == expected_fbc
    # fbp z metadata subskrypcji (snapshot na fakturze) - atrybucja go nie miala
    assert capi_event["user_data"]["fbp"] == "fb.1.555.666"
    assert capi_event["custom_data"] == {
        "value": 1489.0,
        "currency": "pln",
        "content_name": "annual",
    }
    assert capi_event["event_source_url"] == "https://befreeclub.pl/?fbclid=IwAR999"
    [row] = await stored_events(db_maker)
    assert row.processed_at is not None


@respx.mock
async def test_invoice_paid_renewal_stored_without_capi(
    api_client, db_maker, meta_capi_env, pause_state_mock
):
    """Odnowienia NIE sa Purchase - tylko zapis eventu (kontrakt 5.3).
    Kazda oplacona faktura probuje za to zdjac pauze ze statusu czlonka
    (naturalne wznowienie po pauzie adminowej, kontrakt #16)."""
    event = make_event("invoice.paid", paid_invoice(billing_reason="subscription_cycle"))

    resp = await post_event(api_client, event)

    assert resp.status_code == 200  # zaden request do Meta (respx by wybuchl)
    assert pause_state_mock == [
        {"email": "jan@x.pl", "paused": False, "by": "stripe-webhook"}
    ]
    [row] = await stored_events(db_maker)
    assert row.processed_at is not None
    assert row.error is None


# ── eventy tylko-zapis ────────────────────────────────────────────────────────


async def test_subscription_updated_stored_only(api_client, db_maker):
    event = make_event(
        "customer.subscription.updated",
        {"id": "sub_1", "object": "subscription", "status": "past_due"},
    )

    resp = await post_event(api_client, event)

    assert resp.status_code == 200
    [row] = await stored_events(db_maker)
    assert row.type == "customer.subscription.updated"
    assert row.processed_at is not None
    assert row.payload["data"]["object"]["status"] == "past_due"


# ── capi_events: budowanie fbc (unit) ─────────────────────────────────────────


def test_build_fbc_variants():
    created = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    attr = SimpleNamespace(fbc=None, fbclid="IwAR1", created_at=created)
    ms = int(created.timestamp() * 1000)
    assert capi_events._build_fbc(attr, {}, 1765000000) == f"fb.1.{ms}.IwAR1"

    attr_fbc = SimpleNamespace(fbc="fb.1.1.X", fbclid="IwAR1", created_at=created)
    assert capi_events._build_fbc(attr_fbc, {}, 1765000000) == "fb.1.1.X"

    assert capi_events._build_fbc(None, {"fbclid": "IwAR2"}, 1765000000) == (
        "fb.1.1765000000000.IwAR2"
    )
    assert capi_events._build_fbc(None, {}, 1765000000) is None
