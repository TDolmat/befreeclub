"""Testy toru Klarna ([billing-checkout]): create-klarna-checkout,
confirm-klarna-checkout i WSPOLNY grant_one_time_access (klarna_grant)."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
import respx
from fastapi import HTTPException

from app.core import stripe_client
from app.core.config import settings
from app.modules.billing.models import CheckoutAttribution
from app.modules.billing.schemas import AttributionIn
from app.modules.billing.services import checkout as checkout_service
from app.modules.billing.services import plans as plans_service
from app.modules.billing.services.klarna_grant import (
    PaymentRefundedError,
    add_months_js,
    format_pl_date,
    grant_one_time_access,
)
from app.modules.members.services import provisioning
from app.modules.members.services.provisioning import ProvisionResult

STRIPE = "https://api.stripe.com/v1"
RESEND = "https://api.resend.com/emails"


class FakeSession:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


def make_plan(slug="semiannual", interval="half_year", amount=87900, active=True):
    return SimpleNamespace(
        slug=slug,
        name="Pro",
        stripe_price_id="price_semi",
        stripe_account="current",
        amount_pln=amount,
        interval=interval,
        active=active,
        sort=1,
    )


def form_of(route) -> dict[str, str]:
    body = route.calls.last.request.content.decode()
    return {k: v[0] for k, v in parse_qs(body).items()}


@pytest.fixture
def stripe_current(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_current")
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", None)
    stripe_client.reset_clients()
    yield
    stripe_client.reset_clients()


@pytest.fixture
def plans_db(monkeypatch):
    plans: dict[str, object] = {}

    async def fake_get_by_slug(slug, *, session=None):
        return plans.get(slug)

    monkeypatch.setattr(plans_service, "get_by_slug", fake_get_by_slug)
    return plans


@pytest.fixture
def provision_mock(monkeypatch):
    state = SimpleNamespace(
        calls=[],
        result=ProvisionResult(
            member_id=1, circle_invited=True, circle_member_id="42", already_active=False
        ),
    )

    async def fake_provision(email, name, *, source, expires_at=None, skip_invitation=False):
        state.calls.append(
            {"email": email, "name": name, "source": source, "expires_at": expires_at}
        )
        return state.result

    monkeypatch.setattr(provisioning, "provision", fake_provision)
    return state


def mock_payment_intent(pi_id="pi_1", refunded=False):
    return respx.get(f"{STRIPE}/payment_intents/{pi_id}").respond(
        200,
        json={
            "id": pi_id,
            "object": "payment_intent",
            "latest_charge": {"id": "ch_1", "object": "charge", "refunded": refunded},
        },
    )


# ── add_months_js: arytmetyka Date.setMonth 1:1 ──────────────────────────────


def test_add_months_js_plain():
    assert add_months_js(datetime(2026, 6, 10, 12, 0, tzinfo=UTC), 6) == datetime(
        2026, 12, 10, 12, 0, tzinfo=UTC
    )
    assert add_months_js(datetime(2025, 12, 31, tzinfo=UTC), 12) == datetime(
        2026, 12, 31, tzinfo=UTC
    )


def test_add_months_js_overflow_like_js():
    # 31.03 + 6 mies. -> 01.10 (wrzesien ma 30 dni) - edge case #7 ze speca
    assert add_months_js(datetime(2026, 3, 31, tzinfo=UTC), 6) == datetime(
        2026, 10, 1, tzinfo=UTC
    )
    # 31.01 + 1 mies. -> 03.03 (luty 2026 ma 28 dni), jak Date.setMonth
    assert add_months_js(datetime(2026, 1, 31, tzinfo=UTC), 1) == datetime(
        2026, 3, 3, tzinfo=UTC
    )


def test_format_pl_date():
    assert format_pl_date(datetime(2026, 12, 10, tzinfo=UTC)) == "10 grudnia 2026"


# ── grant_one_time_access (wspolna logika 3 sciezek oryginalu) ───────────────


@respx.mock
async def test_grant_passes_max_candidate_expiry_and_mails_once(
    stripe_current, provision_mock, monkeypatch
):
    """expires_at = teraz + N miesiecy (od POTWIERDZENIA); podbicie w gore
    (max ze starym terminem) to kontrakt members.provision. Mail powitalny
    przy swiezej aktywacji."""
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")
    mock_payment_intent("pi_1", refunded=False)
    mail = respx.post(RESEND).respond(200, json={"id": "email_1"})

    before = datetime.now(UTC)
    result = await grant_one_time_access(
        email="  Jan@X.PL ", duration_months=6, payment_intent_id="pi_1"
    )
    after = datetime.now(UTC)

    assert result.circle_invited is True
    [call] = provision_mock.calls
    assert call["email"] == "jan@x.pl"
    assert call["source"] == "one_time"
    assert add_months_js(before, 6) <= call["expires_at"] <= add_months_js(after, 6)

    # mail powitalny 1:1 ze stripe-webhook (wersja ze zdaniem o ratach)
    assert mail.called
    payload = json.loads(mail.calls.last.request.content)
    assert payload["to"] == ["jan@x.pl"]
    assert payload["subject"] == "Witaj w Be Free Club - dostęp aktywny ✅"
    assert payload["from"] == "Be Free Club <noreply@befreeclub.pl>"
    assert payload["reply_to"] == "krystian@befreeclub.pl"
    assert "Twoja płatność przez Klarna została zaakceptowana." in payload["html"]
    assert "o płatności rat / przedłużenie terminu zajmie się Klarna" in payload["html"]
    assert format_pl_date(call["expires_at"]) in payload["html"]


@respx.mock
async def test_grant_anchors_expiry_to_purchase_date(stripe_current, provision_mock, monkeypatch):
    """Review 2.1: expires_at kotwiczone w purchased_at (session.created),
    nie w "teraz" - reconcile co godzine NIE przedluza dostepu w nieskonczonosc
    (kazdy kolejny grant proponuje TEN SAM termin, max-bump nic nie zmienia)."""
    monkeypatch.setattr(settings, "RESEND_API_KEY", None)
    mock_payment_intent("pi_anchor", refunded=False)
    purchased = datetime(2026, 6, 1, 10, 30, tzinfo=UTC)

    await grant_one_time_access(
        email="jan@x.pl", duration_months=6, payment_intent_id="pi_anchor",
        purchased_at=purchased,
    )
    await grant_one_time_access(
        email="jan@x.pl", duration_months=6, payment_intent_id="pi_anchor",
        purchased_at=purchased,
    )

    expected = add_months_js(purchased, 6)
    assert [c["expires_at"] for c in provision_mock.calls] == [expected, expected]


@respx.mock
async def test_grant_already_active_bumps_without_mail(
    stripe_current, provision_mock, monkeypatch
):
    """Czlonek aktywny: tylko podbicie expires_at (max robi provision),
    BEZ ponownego maila powitalnego - semantyka "mail raz"."""
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")
    provision_mock.result = ProvisionResult(
        member_id=1, circle_invited=False, circle_member_id="42", already_active=True
    )
    mock_payment_intent("pi_2", refunded=False)
    mail = respx.post(RESEND).respond(200, json={"id": "email_x"})

    result = await grant_one_time_access(
        email="jan@x.pl", duration_months=12, payment_intent_id="pi_2"
    )

    assert result.already_active is True
    assert len(provision_mock.calls) == 1  # grant zawsze proponuje nowy termin
    assert not mail.called


@respx.mock
async def test_grant_refused_when_charge_refunded(stripe_current, provision_mock):
    """Naprawa prowizorki #1: zrefundowana platnosc (payment_status dalej
    'paid' na sesji) NIE przywraca dostepu."""
    mock_payment_intent("pi_3", refunded=True)

    with pytest.raises(PaymentRefundedError):
        await grant_one_time_access(
            email="jan@x.pl", duration_months=6, payment_intent_id="pi_3"
        )
    assert provision_mock.calls == []


# ── create-klarna-checkout -> create_klarna_session ──────────────────────────


@respx.mock
async def test_create_klarna_session_happy_path(stripe_current, plans_db):
    plans_db["semiannual"] = make_plan()
    route = respx.post(f"{STRIPE}/checkout/sessions").respond(
        200,
        json={"id": "cs_1", "object": "checkout.session",
              "url": "https://checkout.stripe.com/c/cs_1"},
    )
    session = FakeSession()

    result = await checkout_service.create_klarna_session(
        session,
        plan_id="semiannual",
        email=None,
        promo_code=None,
        attribution=AttributionIn(utm_source="fb", fbp="fb.1.1.2"),
        origin="https://befreeclub.pl",
        client_ip="1.2.3.4",
        client_ua="UA",
    )

    assert result == {"url": "https://checkout.stripe.com/c/cs_1", "sessionId": "cs_1"}
    form = form_of(route)
    assert form["mode"] == "payment"
    assert form["payment_method_types[0]"] == "klarna"
    assert form["payment_method_types[1]"] == "card"
    assert form["payment_method_types[2]"] == "blik"
    assert form["line_items[0][price_data][currency]"] == "pln"
    assert form["line_items[0][price_data][unit_amount]"] == "87900"
    assert form["line_items[0][price_data][product_data][name]"] == "Be Free Club - 6 miesięcy"
    assert form["line_items[0][price_data][product_data][description]"] == (
        "Dostęp do społeczności Be Free Club przez 6 miesięcy. Płatność jednorazowa."
    )
    assert form["metadata[source]"] == "klarna_checkout"
    assert form["metadata[plan_id]"] == "semiannual"
    assert form["metadata[duration_months]"] == "6"
    assert form["metadata[utm_source]"] == "fb"
    assert form["payment_intent_data[metadata][source]"] == "klarna_checkout"
    assert form["payment_intent_data[metadata][utm_source]"] == "fb"
    assert form["success_url"] == (
        "https://befreeclub.pl/sukces?source=klarna&plan=semiannual"
        "&session_id={CHECKOUT_SESSION_ID}"
    )
    assert form["cancel_url"] == "https://befreeclub.pl/?checkout_failed=true&planId=semiannual"
    assert form["locale"] == "pl"
    assert form["customer_creation"] == "always"  # brak emaila w requestcie
    assert route.calls.last.request.headers["idempotency-key"].startswith("klarna-")

    [row] = session.added
    assert isinstance(row, CheckoutAttribution)
    assert row.kind == "klarna"
    assert row.stripe_object_id == "cs_1"
    assert row.email is None  # autorytatywny email zbiera Stripe (kontrakt 5.1)
    assert session.commits == 1


async def test_create_klarna_session_rejects_quarterly(stripe_current, plans_db):
    plans_db["quarterly"] = make_plan(slug="quarterly", interval="quarter", amount=63900)
    with pytest.raises(HTTPException) as exc:
        await checkout_service.create_klarna_session(
            FakeSession(),
            plan_id="quarterly",
            email=None,
            promo_code=None,
            attribution=None,
            origin=None,
            client_ip=None,
            client_ua=None,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == "Klarna nie jest dostępna dla planu: quarterly"


# ── confirm-klarna-checkout -> confirm_klarna ────────────────────────────────


KLARNA_SESSION_CREATED = int(datetime(2026, 6, 1, 10, 30, tzinfo=UTC).timestamp())


def mock_klarna_session(
    session_id="cs_1",
    payment_status="paid",
    source="klarna_checkout",
    duration="6",
    email="Jan@X.pl",
):
    return respx.get(f"{STRIPE}/checkout/sessions/{session_id}").respond(
        200,
        json={
            "id": session_id,
            "object": "checkout.session",
            "payment_status": payment_status,
            "created": KLARNA_SESSION_CREATED,
            "customer_email": None,
            "customer_details": {"email": email},
            "payment_intent": "pi_1",
            "metadata": {"source": source, "plan_id": "semiannual", "duration_months": duration},
        },
    )


@respx.mock
async def test_confirm_klarna_happy_path(stripe_current, provision_mock, monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", None)  # mail cicho pominiety
    mock_klarna_session()
    mock_payment_intent("pi_1", refunded=False)

    result = await checkout_service.confirm_klarna(FakeSession(), session_id="cs_1")

    assert result == {
        "success": True,
        "email": "jan@x.pl",
        "circleMemberId": "42",
        "paymentIntentId": "pi_1",
    }
    [call] = provision_mock.calls
    assert call["email"] == "jan@x.pl"
    assert call["source"] == "one_time"
    # Review 2.1: termin kotwiczony w created sesji, nie w "teraz".
    assert call["expires_at"] == add_months_js(
        datetime.fromtimestamp(KLARNA_SESSION_CREATED, UTC), 6
    )


@respx.mock
async def test_confirm_klarna_already_active(stripe_current, provision_mock, monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", None)
    provision_mock.result = ProvisionResult(
        member_id=1, circle_invited=False, circle_member_id="42", already_active=True
    )
    mock_klarna_session()
    mock_payment_intent("pi_1", refunded=False)

    result = await checkout_service.confirm_klarna(FakeSession(), session_id="cs_1")

    assert result == {
        "success": True,
        "alreadyActive": True,
        "email": "jan@x.pl",
        "paymentIntentId": "pi_1",
    }


@respx.mock
async def test_confirm_klarna_not_paid_409(stripe_current):
    mock_klarna_session(payment_status="unpaid")
    with pytest.raises(HTTPException) as exc:
        await checkout_service.confirm_klarna(FakeSession(), session_id="cs_1")
    assert exc.value.status_code == 409
    assert exc.value.detail == "Payment is not paid yet: unpaid"


@respx.mock
async def test_confirm_klarna_rejects_non_klarna_session(stripe_current):
    mock_klarna_session(source="ebook")
    with pytest.raises(HTTPException) as exc:
        await checkout_service.confirm_klarna(FakeSession(), session_id="cs_1")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Checkout session is not a Klarna checkout"


@respx.mock
async def test_confirm_klarna_refunded_refused(stripe_current, provision_mock):
    mock_klarna_session()
    mock_payment_intent("pi_1", refunded=True)

    with pytest.raises(HTTPException) as exc:
        await checkout_service.confirm_klarna(FakeSession(), session_id="cs_1")

    assert exc.value.status_code == 409
    assert exc.value.detail == checkout_service.REFUNDED_MESSAGE
    assert provision_mock.calls == []


@respx.mock
async def test_confirm_klarna_invite_failed_502(stripe_current, provision_mock, monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", None)
    provision_mock.result = ProvisionResult(
        member_id=1, circle_invited=False, circle_member_id=None, already_active=False
    )
    mock_klarna_session()
    mock_payment_intent("pi_1", refunded=False)

    with pytest.raises(HTTPException) as exc:
        await checkout_service.confirm_klarna(FakeSession(), session_id="cs_1")
    assert exc.value.status_code == 502
    assert exc.value.detail == "Circle invite failed"


async def test_confirm_klarna_missing_session_id(stripe_current):
    with pytest.raises(HTTPException) as exc:
        await checkout_service.confirm_klarna(FakeSession(), session_id=None)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Missing sessionId"
