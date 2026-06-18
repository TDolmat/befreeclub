"""Testy portu checkoutu subskrypcji ([billing-checkout]).

Stripe mockowany przez respx (HTTPXClient SDK przechodzi przez httpx),
DB przez FakeSession (bez Postgresa), members.provision przez monkeypatch.
"""

from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from fastapi import HTTPException

from app.core import stripe_client
from app.core.config import settings
from app.modules.billing.models import CheckoutAttribution
from app.modules.billing.schemas import AttributionIn
from app.modules.billing.services import checkout as checkout_service
from app.modules.billing.services import plans as plans_service
from app.modules.billing.services import rate_limit as checkout_rate_limit
from app.modules.members.services import provisioning
from app.modules.members.services.provisioning import ProvisionResult

STRIPE = "https://api.stripe.com/v1"


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


def make_plan(
    slug="semiannual",
    interval="half_year",
    price_id="price_semi",
    amount=87900,
    active=True,
):
    return SimpleNamespace(
        slug=slug,
        name="Pro",
        stripe_price_id=price_id,
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


@pytest.fixture
def no_attribution_row(monkeypatch):
    async def fake_load(session, stripe_object_id):
        return None

    monkeypatch.setattr(checkout_service, "_load_attribution", fake_load)


def list_json(data):
    return {"object": "list", "data": data, "has_more": False}


def search_json(data):
    return {
        "object": "search_result",
        "data": data,
        "has_more": False,
        "url": "/v1/customers/search",
    }


def mock_customers_search(data=None):
    """Fallback customers.search (case-insensitive) - wolany gdy list pusty."""
    return respx.get(f"{STRIPE}/customers/search").respond(200, json=search_json(data or []))


def active_sub(sub_id: str, price_id: str) -> dict:
    return {
        "id": sub_id,
        "object": "subscription",
        "status": "active",
        "items": {
            "object": "list",
            "data": [
                {
                    "id": f"si-item-{sub_id}",
                    "object": "subscription_item",
                    "price": {"id": price_id, "object": "price"},
                }
            ],
        },
    }


# ── POST /checkout/setup-intent (port create-checkout) ───────────────────────


@respx.mock
async def test_setup_intent_happy_path(stripe_current, plans_db):
    plans_db["semiannual"] = make_plan()
    route = respx.post(f"{STRIPE}/setup_intents").respond(
        200,
        json={"id": "seti_9", "object": "setup_intent", "client_secret": "seti_9_secret_x"},
    )
    session = FakeSession()

    result = await checkout_service.create_setup_intent(
        session,
        plan_id="semiannual",
        attribution=AttributionIn(utm_source="instagram", fbclid="fb123"),
        client_ip="1.2.3.4",
        client_ua="UA",
    )

    assert result == {"clientSecret": "seti_9_secret_x", "setupIntentId": "seti_9"}
    form = form_of(route)
    assert form["usage"] == "off_session"
    assert form["payment_method_types[0]"] == "card"
    assert form["metadata[price_id]"] == "price_semi"
    assert form["metadata[plan_id]"] == "semiannual"
    assert form["metadata[utm_source]"] == "instagram"
    assert form["metadata[fbclid]"] == "fb123"
    idem = route.calls.last.request.headers["idempotency-key"]
    assert idem.startswith("checkout-setup-")

    # KONTRAKT 5.1: setup-intent NIE zapisuje atrybucji (prefetch frontu
    # nie smieci tabela) - wiersz tworzy dopiero confirm.
    assert session.added == []
    assert session.commits == 0


async def test_setup_intent_invalid_plan(stripe_current, plans_db):
    with pytest.raises(HTTPException) as exc:
        await checkout_service.create_setup_intent(
            FakeSession(), plan_id="bogus", attribution=None, client_ip=None, client_ua=None
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid plan: bogus"


async def test_setup_intent_inactive_plan_rejected(stripe_current, plans_db):
    plans_db["semiannual"] = make_plan(active=False)
    with pytest.raises(HTTPException) as exc:
        await checkout_service.create_setup_intent(
            FakeSession(), plan_id="semiannual", attribution=None, client_ip=None, client_ua=None
        )
    assert exc.value.status_code == 400


# ── POST /checkout/confirm (port confirm-subscription) ───────────────────────


@respx.mock
async def test_confirm_happy_path(stripe_current, plans_db, provision_mock, no_attribution_row):
    plans_db["semiannual"] = make_plan()
    respx.get(f"{STRIPE}/setup_intents/seti_1").respond(
        200, json={"id": "seti_1", "object": "setup_intent", "status": "succeeded",
                   "payment_method": "pm_1"},
    )
    respx.get(f"{STRIPE}/payment_methods/pm_1").respond(
        200, json={"id": "pm_1", "object": "payment_method", "customer": None}
    )
    respx.get(f"{STRIPE}/customers").respond(200, json=list_json([]))
    mock_customers_search()  # list pusty -> fallback search tez pusty
    create_customer = respx.post(f"{STRIPE}/customers").respond(
        200, json={"id": "cus_new", "object": "customer", "email": "jan@x.pl", "metadata": {}}
    )
    attach = respx.post(f"{STRIPE}/payment_methods/pm_1/attach").respond(
        200, json={"id": "pm_1", "object": "payment_method", "customer": "cus_new"}
    )
    respx.get(f"{STRIPE}/subscriptions").respond(200, json=list_json([]))
    update_customer = respx.post(f"{STRIPE}/customers/cus_new").respond(
        200, json={"id": "cus_new", "object": "customer"}
    )
    create_sub = respx.post(f"{STRIPE}/subscriptions").respond(
        200, json={"id": "sub_1", "object": "subscription", "status": "active",
                   "latest_invoice": "in_1"},
    )
    session = FakeSession()

    result = await checkout_service.confirm_subscription(
        session,
        setup_intent_id="seti_1",
        plan_id="semiannual",
        email="  Jan@X.PL ",
        want_invoice=False,
        nip=None,
        promo_code=None,
        attribution=AttributionIn(utm_source="instagram", fbclid="fb123"),
        client_ip="1.2.3.4",
        client_ua="UA",
    )

    assert result == {
        "subscriptionId": "sub_1",
        "status": "active",
        "circleInvited": True,
        "latestInvoiceId": "in_1",
    }
    assert create_customer.called
    assert attach.called
    assert update_customer.called  # invoice_settings.default_payment_method

    form = form_of(create_sub)
    assert form["customer"] == "cus_new"
    assert form["items[0][price]"] == "price_semi"
    assert form["default_payment_method"] == "pm_1"
    assert form["payment_behavior"] == "error_if_incomplete"
    assert form["off_session"] == "true"
    assert form["payment_settings[save_default_payment_method]"] == "on_subscription"
    assert form["metadata[plan_id]"] == "semiannual"
    assert form["metadata[utm_source]"] == "instagram"
    assert form["metadata[fbclid]"] == "fb123"
    assert create_sub.calls.last.request.headers["idempotency-key"] == "sub-create-seti_1"

    # provision zamiast copy-paste inviteToCircle, email znormalizowany
    assert provision_mock.calls == [
        {"email": "jan@x.pl", "name": None, "source": "subscription", "expires_at": None}
    ]
    # atrybucja: zapis robi confirm (kontrakt 5.1) z emailem
    [row] = session.added
    assert isinstance(row, CheckoutAttribution)
    assert row.email == "jan@x.pl"
    assert row.stripe_object_id == "seti_1"
    assert row.utm_source == "instagram"


@respx.mock
async def test_confirm_pm_attached_to_foreign_customer(
    stripe_current, plans_db, provision_mock, no_attribution_row
):
    """Apple/Google Pay: PM przypiety do INNEGO customera -> uzywamy TEGO
    customera, ignorujac email z requestu (1:1 ze specem, sekcja 4.3 5a)."""
    plans_db["semiannual"] = make_plan()
    respx.get(f"{STRIPE}/setup_intents/seti_2").respond(
        200, json={"id": "seti_2", "object": "setup_intent", "status": "succeeded",
                   "payment_method": "pm_2"},
    )
    respx.get(f"{STRIPE}/payment_methods/pm_2").respond(
        200, json={"id": "pm_2", "object": "payment_method", "customer": "cus_other"}
    )
    respx.get(f"{STRIPE}/customers/cus_other").respond(
        200, json={"id": "cus_other", "object": "customer", "email": "other@x.pl",
                   "metadata": {}},
    )
    # list po emailu zwraca INNEGO customera - ma zostac zignorowany
    respx.get(f"{STRIPE}/customers").respond(
        200, json=list_json([{"id": "cus_mail", "object": "customer", "email": "jan@x.pl"}])
    )
    attach = respx.post(f"{STRIPE}/payment_methods/pm_2/attach").respond(
        200, json={"id": "pm_2", "object": "payment_method"}
    )
    respx.get(f"{STRIPE}/subscriptions").respond(200, json=list_json([]))
    respx.post(f"{STRIPE}/customers/cus_other").respond(
        200, json={"id": "cus_other", "object": "customer"}
    )
    create_sub = respx.post(f"{STRIPE}/subscriptions").respond(
        200, json={"id": "sub_2", "object": "subscription", "status": "active",
                   "latest_invoice": "in_2"},
    )

    result = await checkout_service.confirm_subscription(
        FakeSession(),
        setup_intent_id="seti_2",
        plan_id="semiannual",
        email="jan@x.pl",
        want_invoice=False,
        nip=None,
        promo_code=None,
        attribution=None,
        client_ip=None,
        client_ua=None,
    )

    assert result["subscriptionId"] == "sub_2"
    assert form_of(create_sub)["customer"] == "cus_other"
    assert not attach.called  # PM juz wisi na customerze - bez attach
    # Circle invite idzie na email z formularza (nie z customera Stripe)
    assert provision_mock.calls[0]["email"] == "jan@x.pl"


@respx.mock
async def test_confirm_idempotent_same_plan(
    stripe_current, plans_db, provision_mock, no_attribution_row
):
    plans_db["semiannual"] = make_plan()
    respx.get(f"{STRIPE}/setup_intents/seti_3").respond(
        200, json={"id": "seti_3", "object": "setup_intent", "status": "succeeded",
                   "payment_method": "pm_3"},
    )
    respx.get(f"{STRIPE}/payment_methods/pm_3").respond(
        200, json={"id": "pm_3", "object": "payment_method", "customer": None}
    )
    respx.get(f"{STRIPE}/customers").respond(
        200, json=list_json([{"id": "cus_1", "object": "customer", "email": "jan@x.pl"}])
    )
    respx.post(f"{STRIPE}/payment_methods/pm_3/attach").respond(
        200, json={"id": "pm_3", "object": "payment_method"}
    )
    respx.get(f"{STRIPE}/subscriptions").respond(
        200, json=list_json([active_sub("sub_existing", "price_semi")])
    )
    create_sub = respx.post(f"{STRIPE}/subscriptions").respond(200, json={})

    result = await checkout_service.confirm_subscription(
        FakeSession(),
        setup_intent_id="seti_3",
        plan_id="semiannual",
        email="jan@x.pl",
        want_invoice=False,
        nip=None,
        promo_code=None,
        attribution=None,
        client_ip=None,
        client_ua=None,
    )

    # 1:1: sukces bez akcji, bez nowej suby, bez ponownego invite
    assert result == {"subscriptionId": "sub_existing", "status": "active", "alreadyExisted": True}
    assert not create_sub.called
    assert provision_mock.calls == []


@respx.mock
async def test_confirm_blocks_second_plan(
    stripe_current, plans_db, provision_mock, no_attribution_row
):
    """ROZSZERZENIE portu (dlug #12): aktywna suba INNEGO planu = 409 zamiast
    drugiej rownoleglej subskrypcji i podwojnych obciazen."""
    plans_db["semiannual"] = make_plan()
    respx.get(f"{STRIPE}/setup_intents/seti_4").respond(
        200, json={"id": "seti_4", "object": "setup_intent", "status": "succeeded",
                   "payment_method": "pm_4"},
    )
    respx.get(f"{STRIPE}/payment_methods/pm_4").respond(
        200, json={"id": "pm_4", "object": "payment_method", "customer": None}
    )
    respx.get(f"{STRIPE}/customers").respond(
        200, json=list_json([{"id": "cus_1", "object": "customer", "email": "jan@x.pl"}])
    )
    respx.post(f"{STRIPE}/payment_methods/pm_4/attach").respond(
        200, json={"id": "pm_4", "object": "payment_method"}
    )
    respx.get(f"{STRIPE}/subscriptions").respond(
        200, json=list_json([active_sub("sub_other_plan", "price_annual")])
    )
    create_sub = respx.post(f"{STRIPE}/subscriptions").respond(200, json={})

    with pytest.raises(HTTPException) as exc:
        await checkout_service.confirm_subscription(
            FakeSession(),
            setup_intent_id="seti_4",
            plan_id="semiannual",
            email="jan@x.pl",
            want_invoice=False,
            nip=None,
            promo_code=None,
            attribution=None,
            client_ip=None,
            client_ua=None,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == checkout_service.SECOND_PLAN_MESSAGE
    assert not create_sub.called
    assert provision_mock.calls == []


@respx.mock
async def test_confirm_finds_legacy_mixed_case_customer_via_search(
    stripe_current, plans_db, provision_mock, no_attribution_row
):
    """REGRESJA (review 2.1, HIGH): filtr email w customers.list jest
    case-sensitive - customer zalozony jako "Jan@X.pl" nie wpada w list po
    znormalizowanym emailu. Fallback customers.search MUSI go znalezc,
    inaczej powstaje duplikat customera i DRUGA pelnoplatna suba."""
    plans_db["semiannual"] = make_plan()
    respx.get(f"{STRIPE}/setup_intents/seti_8").respond(
        200, json={"id": "seti_8", "object": "setup_intent", "status": "succeeded",
                   "payment_method": "pm_8"},
    )
    respx.get(f"{STRIPE}/payment_methods/pm_8").respond(
        200, json={"id": "pm_8", "object": "payment_method", "customer": None}
    )
    respx.get(f"{STRIPE}/customers").respond(200, json=list_json([]))  # list: miss
    mock_customers_search(
        [{"id": "cus_legacy_case", "object": "customer", "email": "Jan@X.pl"}]
    )
    create_customer = respx.post(f"{STRIPE}/customers").respond(200, json={})
    respx.post(f"{STRIPE}/payment_methods/pm_8/attach").respond(
        200, json={"id": "pm_8", "object": "payment_method"}
    )
    # Istniejaca aktywna suba TEGO SAMEGO planu -> idempotentny alreadyExisted.
    respx.get(f"{STRIPE}/subscriptions").respond(
        200, json=list_json([active_sub("sub_old", "price_semi")])
    )
    create_sub = respx.post(f"{STRIPE}/subscriptions").respond(200, json={})

    result = await checkout_service.confirm_subscription(
        FakeSession(),
        setup_intent_id="seti_8",
        plan_id="semiannual",
        email="jan@x.pl",
        want_invoice=False,
        nip=None,
        promo_code=None,
        attribution=None,
        client_ip=None,
        client_ua=None,
    )

    assert result == {"subscriptionId": "sub_old", "status": "active", "alreadyExisted": True}
    assert not create_customer.called  # ZERO duplikatu customera
    assert not create_sub.called  # ZERO drugiej suby
    assert provision_mock.calls == []


@respx.mock
async def test_confirm_blocks_trialing_sub_email_wide(
    stripe_current, plans_db, provision_mock, no_attribution_row
):
    """REGRESJA (review 2.1): pauza/przedluzenie adminowe dzialaja przez
    trial_end - spauzowany czlonek (status trialing) kupujacy ponownie
    dostaje 409, nie druga pelnoplatna sube."""
    plans_db["semiannual"] = make_plan()
    respx.get(f"{STRIPE}/setup_intents/seti_7").respond(
        200, json={"id": "seti_7", "object": "setup_intent", "status": "succeeded",
                   "payment_method": "pm_7"},
    )
    respx.get(f"{STRIPE}/payment_methods/pm_7").respond(
        200, json={"id": "pm_7", "object": "payment_method", "customer": None}
    )
    respx.get(f"{STRIPE}/customers").respond(
        200, json=list_json([{"id": "cus_1", "object": "customer", "email": "jan@x.pl"}])
    )
    respx.post(f"{STRIPE}/payment_methods/pm_7/attach").respond(
        200, json={"id": "pm_7", "object": "payment_method"}
    )
    trialing = {**active_sub("sub_trial", "price_semi"), "status": "trialing"}
    # Per-customer check pyta status=active (pusto); guard po emailu pyta
    # status=all i widzi sube trialing.
    respx.get(f"{STRIPE}/subscriptions", params={"status": "active"}).respond(
        200, json=list_json([])
    )
    respx.get(f"{STRIPE}/subscriptions", params={"status": "all"}).respond(
        200, json=list_json([trialing])
    )
    create_sub = respx.post(f"{STRIPE}/subscriptions").respond(200, json={})

    with pytest.raises(HTTPException) as exc:
        await checkout_service.confirm_subscription(
            FakeSession(),
            setup_intent_id="seti_7",
            plan_id="semiannual",
            email="jan@x.pl",
            want_invoice=False,
            nip=None,
            promo_code=None,
            attribution=None,
            client_ip=None,
            client_ua=None,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == checkout_service.SECOND_PLAN_MESSAGE
    assert not create_sub.called


async def test_legacy_plan_not_sellable(stripe_current, plans_db):
    """Guard (review 2.1): plan zaseedowany na koncie legacy nie sprzedaje
    (promo lookup/confirm Klarny/fulfillment sa current-only)."""
    plan = make_plan()
    plan.stripe_account = "legacy"
    plans_db["semiannual"] = plan
    with pytest.raises(HTTPException) as exc:
        await checkout_service.create_setup_intent(
            FakeSession(), plan_id="semiannual", attribution=None, client_ip=None, client_ua=None
        )
    assert exc.value.status_code == 500
    assert "legacy" in exc.value.detail


@respx.mock
async def test_confirm_setup_intent_not_succeeded(stripe_current, plans_db):
    plans_db["semiannual"] = make_plan()
    respx.get(f"{STRIPE}/setup_intents/seti_5").respond(
        200, json={"id": "seti_5", "object": "setup_intent",
                   "status": "requires_payment_method", "payment_method": None},
    )

    with pytest.raises(HTTPException) as exc:
        await checkout_service.confirm_subscription(
            FakeSession(),
            setup_intent_id="seti_5",
            plan_id="semiannual",
            email="jan@x.pl",
            want_invoice=False,
            nip=None,
            promo_code=None,
            attribution=None,
            client_ip=None,
            client_ua=None,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "SetupIntent not succeeded: requires_payment_method"


async def test_confirm_missing_fields(stripe_current):
    with pytest.raises(HTTPException) as exc:
        await checkout_service.confirm_subscription(
            FakeSession(),
            setup_intent_id="seti_1",
            plan_id=None,
            email="jan@x.pl",
            want_invoice=False,
            nip=None,
            promo_code=None,
            attribution=None,
            client_ip=None,
            client_ua=None,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == "Missing required fields: setupIntentId, planId, email"


# ── rate limit checkoutu (kontrakt 1.4: 30 prob / 15 min) ────────────────────


def test_checkout_rate_limit_locks_after_30():
    checkout_rate_limit.reset()
    request = SimpleNamespace(headers=httpx.Headers({"x-real-ip": "9.9.9.9"}))
    try:
        for _ in range(checkout_rate_limit.MAX_FAILURES):
            checkout_rate_limit.enforce(request, "checkout-setup-intent")
        with pytest.raises(HTTPException) as exc:
            checkout_rate_limit.enforce(request, "checkout-setup-intent")
        assert exc.value.status_code == 429
        assert exc.value.detail == "Zbyt wiele prób. Spróbuj ponownie później."
        # inny endpoint = osobny bucket
        checkout_rate_limit.enforce(request, "checkout-klarna")
    finally:
        checkout_rate_limit.reset()
