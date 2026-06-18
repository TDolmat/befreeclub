"""Testy [billing-ebook]: idempotencja fulfillmentu (sekwencyjna i rownolegla),
atomowy licznik pobran (wyscig o ostatni slot), wygasle/uniewaznione tokeny,
endpointy HTTP (payment-intent z atrybucja, confirm, download).

Wymagaja lokalnego Postgresa (DB_HOST/DB_PORT/DB_USER z env) - tworza wlasna
baze `befreeclub_ebook_test` (drop + create na starcie modulu). Stripe i Resend
mockowane przez respx.
"""

import asyncio
import json
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, quote

import httpx
import pytest
import respx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import stripe_client
from app.core.config import settings
from app.core.db import Base, get_session
from app.main import app
from app.modules.billing.models import (
    CheckoutAttribution,
    EbookDownloadToken,
    EbookOrder,
    Plan,
)
from app.modules.billing.services import ebook as ebook_service
from app.modules.billing.services.ebook import (
    ConsumedDownload,
    DownloadTokenError,
    EbookFulfillmentError,
    consume_download_token,
    fulfill_ebook_order,
    invalidate_ebook_tokens,
)

TEST_DB = "befreeclub_ebook_test"
RESEND_URL = "https://api.resend.com/emails"
STRIPE_PI_URL = "https://api.stripe.com/v1/payment_intents"

TABLES = [
    Plan.__table__,
    CheckoutAttribution.__table__,
    EbookOrder.__table__,
    EbookDownloadToken.__table__,
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
        for ddl in ENUM_DDL:
            await conn.execute(text(ddl))
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
        await conn.execute(
            text(
                "INSERT INTO billing.plans "
                '(slug, name, stripe_price_id, amount_pln, "interval", sort) '
                "VALUES ('ebook', 'Ebook: Na swoich zasadach jako freelancer', "
                "'price_test_ebook', 24900, 'one_time', 4)"
            )
        )
    await engine.dispose()


@pytest.fixture(scope="module")
def ebook_db():
    asyncio.run(_create_test_db())
    yield


@pytest.fixture
async def db_maker(ebook_db, monkeypatch):
    """Sessionmaker do testowej bazy na biezacym loopie + patch serwisu."""
    engine = create_async_engine(_dsn(TEST_DB))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        await s.execute(
            text(
                "TRUNCATE billing.ebook_download_tokens, billing.ebook_orders, "
                "billing.checkout_attributions"
            )
        )
        await s.commit()
    monkeypatch.setattr(ebook_service, "async_session_maker", maker)
    yield maker
    await engine.dispose()


@pytest.fixture
def resend_key(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")


@pytest.fixture
def ebook_file(tmp_path, monkeypatch):
    f = tmp_path / "ebook.pdf"
    f.write_bytes(b"%PDF-1.4 test")
    monkeypatch.setattr(settings, "EBOOK_FILE_PATH", str(f))
    return f


@pytest.fixture
async def api_client(db_maker, monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_x")
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


def make_pi(
    pi_id: str = "pi_test_1",
    status: str = "succeeded",
    receipt_email: str | None = "kupujacy@x.pl",
    metadata: dict | None = None,
    amount: int = 24900,
) -> dict:
    return {
        "id": pi_id,
        "object": "payment_intent",
        "status": status,
        "amount": amount,
        "currency": "pln",
        "receipt_email": receipt_email,
        "metadata": metadata if metadata is not None else {"product": "ebook"},
        "client_secret": f"{pi_id}_secret_x",
    }


async def _make_token(
    maker,
    *,
    count: int = 0,
    max_downloads: int = 10,
    expires_delta: timedelta = timedelta(days=30),
    revoked: bool = False,
) -> str:
    token_value = secrets.token_hex(32)
    async with maker() as s:
        order = EbookOrder(
            email="kupujacy@x.pl",
            stripe_payment_intent_id=f"pi_{token_value[:16]}",
            amount_paid=24900,
            status="paid",
        )
        s.add(order)
        await s.flush()
        s.add(
            EbookDownloadToken(
                order_id=order.id,
                token=token_value,
                email="kupujacy@x.pl",
                expires_at=datetime.now(UTC) + expires_delta,
                download_count=count,
                max_downloads=max_downloads,
                revoked_at=datetime.now(UTC) if revoked else None,
            )
        )
        await s.commit()
    return token_value


# ── fulfill_ebook_order ───────────────────────────────────────────────────────


@respx.mock
async def test_fulfill_creates_order_token_and_mail(db_maker, resend_key, monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", None)
    mail = respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "m1"}))

    result = await fulfill_ebook_order(make_pi())

    assert result.email == "kupujacy@x.pl"
    assert len(result.token) == 64
    assert result.email_sent is True
    assert result.download_url == f"https://befreeclub.pl/ebook/pobierz?token={result.token}"

    body = json.loads(mail.calls.last.request.content)
    assert body["to"] == ["kupujacy@x.pl"]
    assert body["subject"] == "Twój ebook jest gotowy do pobrania 📘"
    assert result.download_url in body["html"]
    assert "Dzięki za zakup!" in body["html"]

    async with db_maker() as s:
        order = (await s.execute(select(EbookOrder))).scalar_one()
        token_row = (await s.execute(select(EbookDownloadToken))).scalar_one()
    assert order.status == "paid"
    assert order.email == "kupujacy@x.pl"
    assert order.amount_paid == 24900
    assert order.email_sent_at is not None
    assert order.paid_at is not None
    assert token_row.order_id == order.id
    assert token_row.token == result.token
    assert token_row.max_downloads == 10
    assert abs((token_row.expires_at - datetime.now(UTC)).days - 30) <= 1


@respx.mock
async def test_fulfill_idempotent_sequential(db_maker, resend_key):
    mail = respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "m1"}))

    first = await fulfill_ebook_order(make_pi())
    second = await fulfill_ebook_order(make_pi())

    assert first.token == second.token
    assert first.email_sent is True
    assert second.email_sent is False  # mail tylko raz (guard email_sent_at)
    assert mail.call_count == 1
    async with db_maker() as s:
        orders = (await s.execute(select(func.count()).select_from(EbookOrder))).scalar_one()
        tokens = (
            await s.execute(select(func.count()).select_from(EbookDownloadToken))
        ).scalar_one()
    assert orders == 1
    assert tokens == 1


@respx.mock
async def test_fulfill_idempotent_parallel(db_maker, resend_key):
    """Wyscig confirm vs webhook: jeden order, jeden token, jeden mail."""
    mail = respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "m1"}))

    r1, r2 = await asyncio.gather(
        fulfill_ebook_order(make_pi()), fulfill_ebook_order(make_pi())
    )

    assert r1.token == r2.token
    assert mail.call_count == 1
    async with db_maker() as s:
        orders = (await s.execute(select(func.count()).select_from(EbookOrder))).scalar_one()
        tokens = (
            await s.execute(select(func.count()).select_from(EbookDownloadToken))
        ).scalar_one()
    assert orders == 1
    assert tokens == 1


async def test_fulfill_requires_email(db_maker):
    with pytest.raises(EbookFulfillmentError, match="Missing email"):
        await fulfill_ebook_order(make_pi(receipt_email=None))


async def test_fulfill_rejects_not_succeeded(db_maker):
    with pytest.raises(EbookFulfillmentError, match="Payment status: processing"):
        await fulfill_ebook_order(make_pi(status="processing"))


@respx.mock
async def test_fulfill_client_email_priority_and_invoice_body(db_maker, resend_key):
    """Email klienta wygrywa z receipt_email (1:1); dane fakturowe z body."""
    respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "m1"}))

    result = await fulfill_ebook_order(
        make_pi(receipt_email="stripe@x.pl"),
        email="  Klient@X.PL ",
        wants_invoice=True,
        nip="123-456 78-90",
        invoice_name="Firma Sp. z o.o.",
    )

    assert result.email == "klient@x.pl"
    async with db_maker() as s:
        order = (await s.execute(select(EbookOrder))).scalar_one()
    assert order.email == "klient@x.pl"
    assert order.wants_invoice is True
    assert order.nip == "1234567890"  # NIP czyszczony z [\s-]
    assert order.invoice_name == "Firma Sp. z o.o."


@respx.mock
async def test_fulfill_invoice_from_pi_metadata(db_maker, resend_key):
    """Webhook-first: dane fakturowe z metadata PI, bez powrotu przegladarki."""
    respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "m1"}))
    pi = make_pi(
        metadata={
            "product": "ebook",
            "wants_invoice": "true",
            "nip": "111-222-33-44",
            "invoice_name": "ACME",
        }
    )

    await fulfill_ebook_order(pi)

    async with db_maker() as s:
        order = (await s.execute(select(EbookOrder))).scalar_one()
    assert order.wants_invoice is True
    assert order.nip == "1112223344"
    assert order.invoice_name == "ACME"


@respx.mock
async def test_fulfill_mail_failure_then_retry(db_maker, resend_key):
    """Blad Resend nie psuje fulfillmentu (1:1); mail ponawia sie przy
    nastepnym wywolaniu, bo email_sent_at zostal NULL."""
    mail = respx.post(RESEND_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))

    first = await fulfill_ebook_order(make_pi())
    assert first.email_sent is False

    mail.mock(return_value=httpx.Response(200, json={"id": "m1"}))
    second = await fulfill_ebook_order(make_pi())
    assert second.email_sent is True
    assert second.token == first.token


# ── invalidate_ebook_tokens ───────────────────────────────────────────────────


@respx.mock
async def test_invalidate_by_pi_blocks_refulfill(db_maker, resend_key):
    respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "m1"}))
    await fulfill_ebook_order(make_pi())

    assert await invalidate_ebook_tokens(payment_intent_id="pi_test_1") == 1
    assert await invalidate_ebook_tokens(payment_intent_id="pi_test_1") == 0  # idempotentne

    async with db_maker() as s:
        order = (await s.execute(select(EbookOrder))).scalar_one()
        token_row = (await s.execute(select(EbookDownloadToken))).scalar_one()
    assert order.status == "refunded"
    assert token_row.revoked_at is not None

    # Confirm po refundzie nie wskrzesza zamowienia ani nie wydaje nowego tokenu.
    with pytest.raises(EbookFulfillmentError) as err:
        await fulfill_ebook_order(make_pi())
    assert err.value.status == 409


@respx.mock
async def test_invalidate_by_email(db_maker, resend_key):
    respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "m1"}))
    await fulfill_ebook_order(make_pi(pi_id="pi_a"))
    await fulfill_ebook_order(make_pi(pi_id="pi_b"))

    assert await invalidate_ebook_tokens(email=" Kupujacy@X.PL ") == 2

    async with db_maker() as s:
        statuses = (await s.execute(select(EbookOrder.status))).scalars().all()
    assert statuses == ["refunded", "refunded"]


async def test_invalidate_requires_criterion(db_maker):
    with pytest.raises(ValueError):
        await invalidate_ebook_tokens()


# ── consume_download_token ────────────────────────────────────────────────────


async def test_download_consumes_and_decrements(db_maker, ebook_file):
    token = await _make_token(db_maker)

    consumed = await consume_download_token(token)

    assert consumed.file_path == str(ebook_file)
    assert consumed.filename == "Na-swoich-zasadach-jako-freelancer.pdf"
    assert consumed.remaining_downloads == 9
    async with db_maker() as s:
        row = (
            await s.execute(select(EbookDownloadToken).where(EbookDownloadToken.token == token))
        ).scalar_one()
    assert row.download_count == 1
    assert row.last_downloaded_at is not None


async def test_download_unknown_token(db_maker, ebook_file):
    with pytest.raises(DownloadTokenError) as err:
        await consume_download_token("deadbeef" * 8)
    assert err.value.status == 404
    assert err.value.message == "Nieprawidłowy link"


async def test_download_expired_token(db_maker, ebook_file):
    token = await _make_token(db_maker, expires_delta=timedelta(days=-1))
    with pytest.raises(DownloadTokenError) as err:
        await consume_download_token(token)
    assert err.value.status == 410
    assert err.value.message == "Link wygasł. Napisz na krystian@befreeclub.pl po nowy."


async def test_download_revoked_token(db_maker, ebook_file):
    token = await _make_token(db_maker, revoked=True)
    with pytest.raises(DownloadTokenError) as err:
        await consume_download_token(token)
    assert err.value.status == 410


async def test_download_limit_exhausted(db_maker, ebook_file):
    token = await _make_token(db_maker, count=10)
    with pytest.raises(DownloadTokenError) as err:
        await consume_download_token(token)
    assert err.value.status == 429
    assert err.value.message == "Limit pobrań wyczerpany. Napisz na krystian@befreeclub.pl."


async def test_download_race_for_last_slot(db_maker, ebook_file):
    """Dwa rownolegle pobrania o ostatni slot - atomowy UPDATE przepuszcza
    dokladnie jedno (naprawa dlugu #9: nieatomowy licznik)."""
    token = await _make_token(db_maker, count=9)

    results = await asyncio.gather(
        consume_download_token(token),
        consume_download_token(token),
        return_exceptions=True,
    )

    ok = [r for r in results if isinstance(r, ConsumedDownload)]
    errors = [r for r in results if isinstance(r, DownloadTokenError)]
    assert len(ok) == 1
    assert len(errors) == 1
    assert ok[0].remaining_downloads == 0
    assert errors[0].status == 429
    async with db_maker() as s:
        row = (
            await s.execute(select(EbookDownloadToken).where(EbookDownloadToken.token == token))
        ).scalar_one()
    assert row.download_count == 10  # nie przekroczyl max_downloads


async def test_download_missing_file_does_not_consume(db_maker, monkeypatch):
    monkeypatch.setattr(settings, "EBOOK_FILE_PATH", None)
    token = await _make_token(db_maker)

    with pytest.raises(DownloadTokenError) as err:
        await consume_download_token(token)

    assert err.value.status == 500
    async with db_maker() as s:
        row = (
            await s.execute(select(EbookDownloadToken).where(EbookDownloadToken.token == token))
        ).scalar_one()
    assert row.download_count == 0  # blad pliku nie zuzywa pobrania


# ── endpointy HTTP ────────────────────────────────────────────────────────────


@respx.mock
async def test_payment_intent_route(api_client, db_maker):
    stripe_route = respx.post(STRIPE_PI_URL).mock(
        return_value=httpx.Response(
            200,
            json=make_pi(pi_id="pi_route_1", status="requires_payment_method", receipt_email=None),
        )
    )

    resp = await api_client.post(
        "/api/billing/ebook/payment-intent",
        json={
            "attribution": {
                "utmSource": "ig",
                "utmCampaign": "czerwiec",
                "fbclid": "fb123",
                "fbp": "fb.1.1.2",
                "landingPage": "https://befreeclub.pl/ebook",
            }
        },
        headers={"x-forwarded-for": "10.1.1.1", "user-agent": "pytest-agent"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"clientSecret": "pi_route_1_secret_x", "paymentIntentId": "pi_route_1"}

    request = stripe_route.calls.last.request
    assert "Idempotency-Key" in request.headers
    sent = parse_qs(request.content.decode())
    assert sent["amount"] == ["24900"]  # kwota z billing.plans, nie hardcode
    assert sent["currency"] == ["pln"]
    assert sent["payment_method_types[0]"] == ["card"]
    assert sent["payment_method_types[1]"] == ["blik"]
    assert sent["description"] == ["Ebook: Na swoich zasadach jako freelancer"]
    assert sent["metadata[product]"] == ["ebook"]
    assert sent["metadata[utm_source]"] == ["ig"]
    assert sent["metadata[fbclid]"] == ["fb123"]

    async with db_maker() as s:
        attr = (await s.execute(select(CheckoutAttribution))).scalar_one()
    assert attr.kind == "ebook"
    assert attr.stripe_object_id == "pi_route_1"
    assert attr.email is None  # bez placeholdera - email dopiero przy confirm
    assert attr.utm_source == "ig"
    assert attr.utm_campaign == "czerwiec"
    assert attr.fbclid == "fb123"
    assert attr.fbp == "fb.1.1.2"
    assert attr.landing_page == "https://befreeclub.pl/ebook"
    assert attr.client_ip == "10.1.1.1"
    assert attr.client_ua == "pytest-agent"


@respx.mock
async def test_confirm_route_pending_409(api_client):
    respx.get(f"{STRIPE_PI_URL}/pi_route_2").mock(
        return_value=httpx.Response(200, json=make_pi(pi_id="pi_route_2", status="processing"))
    )

    resp = await api_client.post(
        "/api/billing/ebook/confirm", json={"paymentIntentId": "pi_route_2"}
    )

    # 409 + pole `status` 1:1 - front retry'uje 8x2s na tej podstawie.
    assert resp.status_code == 409
    assert resp.json() == {"error": "Payment status: processing", "status": "processing"}


@respx.mock
async def test_confirm_route_success(api_client, db_maker, resend_key):
    respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "m1"}))
    respx.get(f"{STRIPE_PI_URL}/pi_route_3").mock(
        return_value=httpx.Response(200, json=make_pi(pi_id="pi_route_3"))
    )

    resp = await api_client.post(
        "/api/billing/ebook/confirm",
        json={
            "paymentIntentId": "pi_route_3",
            "email": "Klient@X.PL",
            "wantInvoice": True,
            "nip": "123 456 7890",
            "invoiceName": "ACME",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["email"] == "klient@x.pl"
    assert data["token"] in data["downloadUrl"]

    async with db_maker() as s:
        order = (await s.execute(select(EbookOrder))).scalar_one()
    assert order.status == "paid"
    assert order.stripe_payment_intent_id == "pi_route_3"
    assert order.nip == "1234567890"


async def test_download_route_streams_pdf(api_client, db_maker, ebook_file):
    token = await _make_token(db_maker)

    resp = await api_client.get(f"/api/billing/ebook/download?token={token}")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert (
        'filename="Na-swoich-zasadach-jako-freelancer.pdf"'
        in resp.headers["content-disposition"]
    )
    assert resp.content.startswith(b"%PDF")


async def test_download_route_missing_token(api_client, ebook_file):
    resp = await api_client.get("/api/billing/ebook/download")
    assert resp.status_code == 400
    assert resp.json() == {"error": "Brak tokenu"}
