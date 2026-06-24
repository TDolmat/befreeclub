"""[workers]: warstwa workerow + triggery admina + sweep reconcile Klarny.

- triggery POST /api/billing/admin/workers/{name}/run wolaja logike
  z members/billing (serwisy mockowane),
- reconcile POMIJA sesje z refundowanym charge (naprawa bomby #3
  z PLAN_LANDING) - takze end-to-end przez realny grant + respx,
- filtr sweepa (tylko klarna_checkout + paid), paginacja starting_after,
  fallback duration z billing.plans, wiersze errors.

Stripe mockowany przez respx (SDK uzywa httpx pod spodem). Bez DB - logika
DB zyje w [members]/[billing-checkout] i ma wlasne testy.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from app.core import stripe_client
from app.core.config import settings
from app.modules.billing.services import klarna_reconcile_worker as reconcile
from app.modules.billing.services.klarna_grant import PaymentRefundedError
from app.modules.members.services import cleanup_worker, invite_retry_worker, provisioning
from app.modules.members.services.cleanup import CleanupDecision, CleanupResult
from app.modules.members.services.maintenance import RetryResult
from app.modules.members.services.provisioning import ProvisionResult

STRIPE_SESSIONS = "https://api.stripe.com/v1/checkout/sessions"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings, "NODE_ENV", "development")  # require_auth -> DEV_FAKE_AUTH
    from app.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def stripe_current(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_current")
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", None)
    stripe_client.reset_clients()
    yield
    stripe_client.reset_clients()


SESSION_CREATED_TS = 1_765_000_000


def make_session(
    sid,
    *,
    source="klarna_checkout",
    paid="paid",
    email="user@x.pl",
    duration="6",
    pi="pi_1",
):
    return {
        "id": sid,
        "object": "checkout.session",
        "created": SESSION_CREATED_TS,
        "metadata": {"source": source, "plan_id": "semiannual", "duration_months": duration},
        "payment_status": paid,
        "customer_email": email,
        "customer_details": None,
        "payment_intent": pi,
    }


def list_page(data, has_more=False):
    return {"object": "list", "data": data, "has_more": has_more, "url": "/v1/checkout/sessions"}


# ── Triggery admina (POST /api/billing/admin/workers/{name}/run) ────────────


async def test_trigger_membership_cleanup_calls_service(client, monkeypatch):
    calls = []

    async def fake_run_cleanup(*, dry_run=False):
        calls.append(dry_run)
        return CleanupResult(
            checked=3,
            removed=1,
            would_remove=1,
            dry_run=dry_run,
            decisions=[
                CleanupDecision(
                    member_id=7, email="a@x.pl", decision="subscription_dead", removed=True
                )
            ],
        )

    # admin.settings mockowane (suite bez DB): reczny trigger ignoruje `enabled`,
    # ale czyta dryRun. Tu dryRun=false -> realny przebieg.
    async def fake_get_setting(key):
        return {"enabled": True, "dryRun": False}

    async def fake_set_setting(key, value, user_id):
        return value

    monkeypatch.setattr(cleanup_worker, "run_cleanup", fake_run_cleanup)
    monkeypatch.setattr(cleanup_worker, "get_setting", fake_get_setting)
    monkeypatch.setattr(cleanup_worker, "set_setting", fake_set_setting)

    response = await client.post("/api/billing/admin/workers/membership_cleanup/run")

    assert response.status_code == 200
    assert calls == [False]  # dryRun=false z ustawien -> realny przebieg
    assert response.json() == {
        "success": True,
        "checked": 3,
        "removed": 1,
        "wouldRemove": 1,
        "dryRun": False,
        "decisions": [
            {"memberId": 7, "email": "a@x.pl", "decision": "subscription_dead", "removed": True}
        ],
    }


async def test_trigger_membership_cleanup_honors_dry_run_from_settings(client, monkeypatch):
    """Reczny trigger NIE patrzy na enabled, ale dryRun z ustawien obowiazuje:
    enabled=false + dryRun=true -> przebieg w trybie cienia (zero usuniec)."""
    calls = []

    async def fake_run_cleanup(*, dry_run=False):
        calls.append(dry_run)
        return CleanupResult(checked=2, removed=0, would_remove=1, dry_run=dry_run)

    async def fake_get_setting(key):
        return {"enabled": False, "dryRun": True}

    async def fake_set_setting(key, value, user_id):
        return value

    monkeypatch.setattr(cleanup_worker, "run_cleanup", fake_run_cleanup)
    monkeypatch.setattr(cleanup_worker, "get_setting", fake_get_setting)
    monkeypatch.setattr(cleanup_worker, "set_setting", fake_set_setting)

    response = await client.post("/api/billing/admin/workers/membership_cleanup/run")

    assert response.status_code == 200
    assert calls == [True]  # dryRun=true mimo enabled=false (trigger to akcja czlowieka)
    body = response.json()
    assert body["dryRun"] is True
    assert body["removed"] == 0
    assert body["wouldRemove"] == 1


async def test_trigger_invite_retry(client, monkeypatch):
    async def fake_retry():
        return [
            RetryResult(email="ok@x.pl", success=True, circle_member_id="11"),
            RetryResult(email="bad@x.pl", success=False, error="Circle API 422: nope"),
        ]

    monkeypatch.setattr(invite_retry_worker, "retry_failed_invites", fake_retry)

    response = await client.post("/api/billing/admin/workers/invite_retry/run")

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {"email": "ok@x.pl", "success": True, "circleMemberId": "11", "error": None},
            {
                "email": "bad@x.pl",
                "success": False,
                "circleMemberId": None,
                "error": "Circle API 422: nope",
            },
        ]
    }


async def test_trigger_klarna_reconcile(client, monkeypatch):
    async def fake_reconcile():
        return reconcile.ReconcileSummary(
            scanned=5, klarna_paid=2, newly_invited=1, skipped_refunded=1
        )

    monkeypatch.setattr(reconcile, "run_reconcile", fake_reconcile)

    response = await client.post("/api/billing/admin/workers/klarna_reconcile/run")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "scanned": 5,
        "klarnaPaid": 2,
        "alreadyHandled": 0,
        "newlyInvited": 1,
        "inviteFailed": 0,
        "skippedRefunded": 1,
        "errors": [],
    }


async def test_trigger_unknown_worker_404(client):
    response = await client.post("/api/billing/admin/workers/nope/run")
    assert response.status_code == 404
    assert response.json() == {"error": "Unknown worker: nope"}


async def test_trigger_requires_auth_in_production(monkeypatch):
    monkeypatch.setattr(settings, "NODE_ENV", "production")
    from app.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/api/billing/admin/workers/membership_cleanup/run")

    assert response.status_code == 401
    assert response.json() == {"error": "Unauthorized"}


# ── Sweep reconcile Klarny ───────────────────────────────────────────────────


@respx.mock
async def test_reconcile_skips_refunded_and_filters(stripe_current, monkeypatch):
    sessions = [
        make_session("cs_ok", pi="pi_ok"),
        make_session("cs_refunded", pi="pi_refunded"),
        make_session("cs_ebook", source="ebook_checkout"),
        make_session("cs_unpaid", paid="unpaid"),
    ]
    respx.get(STRIPE_SESSIONS).mock(return_value=httpx.Response(200, json=list_page(sessions)))

    granted = []

    async def fake_grant(*, email, duration_months, payment_intent_id, purchased_at=None):
        if payment_intent_id == "pi_refunded":
            raise PaymentRefundedError(payment_intent_id)
        granted.append((email, duration_months, payment_intent_id, purchased_at))
        return ProvisionResult(
            member_id=1, circle_invited=True, circle_member_id="9", already_active=False
        )

    monkeypatch.setattr(reconcile, "grant_one_time_access", fake_grant)

    summary = await reconcile.run_reconcile()

    assert summary.scanned == 4
    assert summary.klarna_paid == 2  # ebook i unpaid odfiltrowane przed grantem
    assert summary.skipped_refunded == 1
    assert summary.newly_invited == 1
    assert summary.invite_failed == 0
    assert summary.errors == []
    # purchased_at = created sesji (kotwica terminu - review 2.1)
    expected_purchased = datetime.fromtimestamp(SESSION_CREATED_TS, UTC)
    assert granted == [("user@x.pl", 6, "pi_ok", expected_purchased)]


@respx.mock
async def test_reconcile_refund_check_via_stripe(stripe_current, monkeypatch):
    """End-to-end przez REALNY grant_one_time_access: refundowany charge
    z payment_intents.retrieve = zero provisioningu (dostep nie wraca)."""
    respx.get(STRIPE_SESSIONS).mock(
        return_value=httpx.Response(200, json=list_page([make_session("cs_1", pi="pi_x")]))
    )
    respx.get("https://api.stripe.com/v1/payment_intents/pi_x").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "pi_x",
                "object": "payment_intent",
                "latest_charge": {"id": "ch_1", "object": "charge", "refunded": True},
            },
        )
    )

    async def fail_provision(*args, **kwargs):
        raise AssertionError("provision must not run for refunded payment")

    monkeypatch.setattr(provisioning, "provision", fail_provision)

    summary = await reconcile.run_reconcile()

    assert summary.klarna_paid == 1
    assert summary.skipped_refunded == 1
    assert summary.newly_invited == 0
    assert summary.errors == []


@respx.mock
async def test_reconcile_paginates_and_counts(stripe_current, monkeypatch):
    page1 = list_page([make_session("cs_1", pi="pi_1", email="a@x.pl")], has_more=True)
    page2 = list_page(
        [
            make_session("cs_2", pi="pi_2", email="b@x.pl"),
            make_session("cs_3", pi="pi_3", email="c@x.pl"),
        ]
    )
    route = respx.get(STRIPE_SESSIONS).mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )

    results = {
        "a@x.pl": ProvisionResult(1, False, "9", True),  # juz aktywny (bump expires)
        "b@x.pl": ProvisionResult(2, True, "10", False),  # swiezo zaproszony
        "c@x.pl": ProvisionResult(3, False, None, False),  # invite padl
    }

    async def fake_grant(*, email, duration_months, payment_intent_id, purchased_at=None):
        return results[email]

    monkeypatch.setattr(reconcile, "grant_one_time_access", fake_grant)

    summary = await reconcile.run_reconcile()

    assert summary.scanned == 3
    assert summary.klarna_paid == 3
    assert summary.already_handled == 1
    assert summary.newly_invited == 1
    assert summary.invite_failed == 1
    assert route.call_count == 2
    assert "starting_after=cs_1" in str(route.calls.last.request.url)


@respx.mock
async def test_reconcile_errors_and_duration_fallback(stripe_current, monkeypatch):
    sessions = [
        make_session("cs_noemail", email=None),
        make_session("cs_fallback", duration="", pi="pi_f"),
    ]
    respx.get(STRIPE_SESSIONS).mock(return_value=httpx.Response(200, json=list_page(sessions)))

    async def fake_get_by_slug(slug, *, session=None):
        assert slug == "semiannual"
        return SimpleNamespace(interval="half_year")

    monkeypatch.setattr(reconcile.plans_service, "get_by_slug", fake_get_by_slug)

    granted = []

    async def fake_grant(*, email, duration_months, payment_intent_id, purchased_at=None):
        granted.append((email, duration_months, payment_intent_id))
        return ProvisionResult(1, True, "9", False)

    monkeypatch.setattr(reconcile, "grant_one_time_access", fake_grant)

    summary = await reconcile.run_reconcile()

    # Brak emaila -> wiersz errors 1:1 z oryginalem ("Session ...: no email").
    assert summary.errors == ["Session cs_noemail: no email"]
    # Brak duration_months w metadata -> fallback przez billing.plans (6 mies.).
    assert granted == [("user@x.pl", 6, "pi_f")]
    assert summary.newly_invited == 1
