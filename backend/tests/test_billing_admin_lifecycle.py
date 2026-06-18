"""Testy akcji admina na subskrypcjach ([billing-lifecycle]):
pauza (pelne wyszukiwanie), extend (hack trial_end/unpause 1:1),
nowy cancel, historia anulowan, legacy-audit z recent_failures.
"""

import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from fastapi import HTTPException

from app.core import stripe_client
from app.core.config import settings
from app.modules.billing.models import AuditLog, CancellationReason
from app.modules.billing.services import lifecycle_admin as admin_service
from app.modules.billing.services.klarna_grant import add_months_js
from app.modules.members.services import provisioning
from app.modules.members.services.provisioning import RemoveOutcome

STRIPE = "https://api.stripe.com/v1"
CURRENT_H = {"Authorization": "Bearer sk_test_current"}
LEGACY_H = {"Authorization": "Bearer sk_test_legacy"}


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: self._value)


class FakeSession:
    def __init__(self, execute_result=None):
        self.added = []
        self.commits = 0
        self._execute_result = execute_result

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def execute(self, stmt):
        return FakeResult(self._execute_result)


@pytest.fixture
def admin_env(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_current")
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", "sk_test_legacy")
    stripe_client.reset_clients()
    yield
    stripe_client.reset_clients()


@pytest.fixture
def pause_state_mock(monkeypatch):
    """provisioning.set_pause_state (members.status paused/active, kontrakt
    #16) - mock, zeby test nie dotykal realnej DB members."""
    calls: list[dict] = []

    async def fake_set_pause_state(email, paused, *, by=None):
        calls.append({"email": email, "paused": paused, "by": by})
        return True

    monkeypatch.setattr(provisioning, "set_pause_state", fake_set_pause_state)
    return calls


def list_json(data):
    return {"object": "list", "data": data, "has_more": False}


def sub_json(sub_id, status="active", period_end=None, **extra):
    item = {"id": f"si_{sub_id}", "object": "subscription_item"}
    if period_end is not None:
        item["current_period_end"] = period_end
    return {
        "id": sub_id,
        "object": "subscription",
        "status": status,
        "cancel_at_period_end": False,
        "items": {"object": "list", "data": [item]},
        **extra,
    }


def form_of(request) -> dict[str, str]:
    parsed = parse_qs(request.content.decode(), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def search_json(data):
    return {"object": "search_result", "data": data, "has_more": False,
            "url": "/v1/customers/search"}


def mock_no_customers(headers):
    respx.get(f"{STRIPE}/customers", headers=headers).respond(200, json=list_json([]))
    # Pusty list -> kod robi fallback customers.search (case-insensitive).
    respx.get(f"{STRIPE}/customers/search", headers=headers).respond(
        200, json=search_json([])
    )


# ── POST /admin/subscriptions/pause ──────────────────────────────────────────


@respx.mock
async def test_pause_full_search_beyond_first_customer(admin_env, pause_state_mock):
    """Naprawa prowizorki #10: oryginal patrzyl na 1 customera i tylko status
    active. Port znajduje pauzowalna sube (trialing) u DRUGIEGO customera."""
    respx.get(f"{STRIPE}/customers", headers=CURRENT_H).respond(
        200,
        json=list_json(
            [{"id": "cus_a", "object": "customer"}, {"id": "cus_b", "object": "customer"}]
        ),
    )
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H, params={"customer": "cus_a"}).respond(
        200, json=list_json([sub_json("sub_dead", status="canceled")])
    )
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H, params={"customer": "cus_b"}).respond(
        200, json=list_json([sub_json("sub_b", status="trialing")])
    )
    pause = respx.post(f"{STRIPE}/subscriptions/sub_b", headers=CURRENT_H).respond(
        200, json=sub_json("sub_b", status="trialing")
    )
    mock_no_customers(LEGACY_H)
    session = FakeSession()

    result = await admin_service.pause_subscription(
        session,
        email=" Jan@X.PL ",
        freeze_days=14,
        remove_from_circle=False,
        admin_user_id=1,
        admin_email="admin@x.pl",
    )

    assert result["success"] is True
    assert result["email"] == "jan@x.pl"
    assert result["customer_found"] is True
    assert result["stripe"]["current"]["paused"] is True
    assert result["stripe"]["current"]["subscriptionId"] == "sub_b"
    assert result["stripe"]["current"]["customerId"] == "cus_b"
    assert result["stripe"]["legacy"] == {
        "account": "legacy",
        "found": False,
        "paused": False,
        "subscriptionId": None,
        "customerId": None,
        "resumesAt": None,
    }
    assert result["circle"] is None

    form = form_of(pause.calls.last.request)
    assert form["pause_collection[behavior]"] == "void"
    resumes_at = int(form["pause_collection[resumes_at]"])
    assert abs(resumes_at - (int(time.time()) + 14 * 86400)) < 10

    # audyt: cancellation_reasons (admin-pause/frozen) + billing.audit_log
    reasons = [o for o in session.added if isinstance(o, CancellationReason)]
    audits = [o for o in session.added if isinstance(o, AuditLog)]
    assert len(reasons) == 1 and reasons[0].reason == "admin-pause"
    assert reasons[0].action == "frozen" and reasons[0].freeze_days == 14
    assert len(audits) == 1 and audits[0].action == "pause_subscription"
    assert audits[0].admin_user_id == 1 and audits[0].target_email == "jan@x.pl"
    assert session.commits == 1
    # kontrakt #16: members.status -> paused (bez remove_from_circle)
    assert pause_state_mock == [{"email": "jan@x.pl", "paused": True, "by": "admin@x.pl"}]


async def test_pause_validation(admin_env):
    with pytest.raises(HTTPException) as exc:
        await admin_service.pause_subscription(
            FakeSession(),
            email=None,
            freeze_days=14,
            remove_from_circle=False,
            admin_user_id=None,
            admin_email=None,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == "email required"

    with pytest.raises(HTTPException) as exc:
        await admin_service.pause_subscription(
            FakeSession(),
            email="jan@x.pl",
            freeze_days=400,
            remove_from_circle=False,
            admin_user_id=None,
            admin_email=None,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == "freeze_days required (1-365)"


@respx.mock
async def test_pause_remove_from_circle_via_members(admin_env, pause_state_mock, monkeypatch):
    respx.get(f"{STRIPE}/customers", headers=CURRENT_H).respond(
        200, json=list_json([{"id": "cus_1", "object": "customer"}])
    )
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H).respond(
        200, json=list_json([sub_json("sub_1")])
    )
    respx.post(f"{STRIPE}/subscriptions/sub_1").respond(200, json=sub_json("sub_1"))
    mock_no_customers(LEGACY_H)

    async def fake_find_member(session, email):
        return SimpleNamespace(id=7, email=email)

    removed_calls = []

    async def fake_remove_member(member_id, *, reason, by=None):
        removed_calls.append({"member_id": member_id, "reason": reason, "by": by})
        return RemoveOutcome(ok=True, code="removed", circle_removed=True)

    monkeypatch.setattr(admin_service, "_find_member_by_email", fake_find_member)
    monkeypatch.setattr(provisioning, "remove_member", fake_remove_member)

    result = await admin_service.pause_subscription(
        FakeSession(),
        email="jan@x.pl",
        freeze_days=30,
        remove_from_circle=True,
        admin_user_id=None,
        admin_email="admin@x.pl",
    )

    assert result["circle"]["removed"] is True
    assert removed_calls == [{"member_id": 7, "reason": "admin-pause", "by": "admin@x.pl"}]
    # remove_from_circle: status ustawia remove_member, nie set_pause_state
    assert pause_state_mock == []


# ── POST /admin/subscriptions/extend (hack trial_end/unpause 1:1) ────────────


@respx.mock
async def test_extend_hack_sequence(admin_env):
    """Sekwencja 1:1 z oryginalem: (1) zdjecie pauzy pustym stringiem
    + trial_end, (2) ponowne nalozenie pauzy. Stripe nie pozwala ustawic
    trial_end na spauzowanej sub - kolejnosc jest wymuszona."""
    base_ts = int(time.time()) + 30 * 86400
    expected_trial_end = int(
        add_months_js(datetime.fromtimestamp(base_ts, UTC), 2).timestamp()
    )
    resumes_at = int(time.time()) + 60 * 86400

    respx.get(f"{STRIPE}/subscriptions/sub_x", headers=CURRENT_H).respond(
        200,
        json=sub_json(
            "sub_x",
            period_end=base_ts,
            pause_collection={"behavior": "void", "resumes_at": base_ts},
        ),
    )
    update = respx.post(f"{STRIPE}/subscriptions/sub_x", headers=CURRENT_H)
    update.side_effect = [
        httpx.Response(
            200,
            json=sub_json(
                "sub_x", status="trialing", period_end=base_ts, trial_end=expected_trial_end
            ),
        ),
        httpx.Response(
            200,
            json=sub_json(
                "sub_x",
                status="trialing",
                period_end=base_ts,
                trial_end=expected_trial_end,
                pause_collection={"behavior": "void", "resumes_at": resumes_at},
            ),
        ),
    ]
    session = FakeSession()

    result = await admin_service.extend_subscription(
        session,
        subscription_id="sub_x",
        email=None,
        account="current",
        resumes_at=resumes_at,
        trial_end=None,
        add_months=2,
        clear_pause=False,
        admin_user_id=None,
    )

    assert update.call_count == 2
    step1 = form_of(update.calls[0].request)
    assert step1["proration_behavior"] == "none"
    assert step1["pause_collection"] == ""  # hack: pusty string zdejmuje pauze
    assert step1["trial_end"] == str(expected_trial_end)
    step2 = form_of(update.calls[1].request)
    assert step2["pause_collection[behavior]"] == "void"
    assert step2["pause_collection[resumes_at]"] == str(resumes_at)
    assert "trial_end" not in step2

    assert result["success"] is True
    assert result["subscription_id"] == "sub_x"
    assert result["status"] == "trialing"
    assert result["trial_end"] == expected_trial_end
    assert result["trial_end_iso"].endswith("Z")
    assert result["pause_collection"] == {"behavior": "void", "resumes_at": resumes_at}
    assert result["current_period_end"] == base_ts

    audits = [o for o in session.added if isinstance(o, AuditLog)]
    assert len(audits) == 1 and audits[0].action == "extend_subscription"


@respx.mock
async def test_extend_clear_pause_only(admin_env):
    update = respx.post(f"{STRIPE}/subscriptions/sub_y", headers=CURRENT_H).respond(
        200, json=sub_json("sub_y")
    )

    result = await admin_service.extend_subscription(
        FakeSession(),
        subscription_id="sub_y",
        email=None,
        account="current",
        resumes_at=None,
        trial_end=None,
        add_months=None,
        clear_pause=True,
        admin_user_id=None,
    )

    form = form_of(update.calls.last.request)
    assert form == {"proration_behavior": "none", "pause_collection": ""}
    assert result["pause_collection"] is None


async def test_extend_nothing_to_update(admin_env):
    with pytest.raises(HTTPException) as exc:
        await admin_service.extend_subscription(
            FakeSession(),
            subscription_id="sub_z",
            email=None,
            account="current",
            resumes_at=None,
            trial_end=None,
            add_months=None,
            clear_pause=False,
            admin_user_id=None,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == "Nothing to update - provide trial_end, resumes_at, or clear_pause"


@respx.mock
async def test_extend_email_lookup_not_found(admin_env):
    mock_no_customers(CURRENT_H)
    with pytest.raises(HTTPException) as exc:
        await admin_service.extend_subscription(
            FakeSession(),
            subscription_id=None,
            email="nikt@x.pl",
            account="current",
            resumes_at=None,
            trial_end=None,
            add_months=None,
            clear_pause=True,
            admin_user_id=None,
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "No active subscription found for nikt@x.pl on current"


# ── POST /admin/subscriptions/cancel (NOWE) ──────────────────────────────────


@respx.mock
async def test_admin_cancel_immediate_both_accounts(admin_env):
    respx.get(f"{STRIPE}/customers", headers=CURRENT_H).respond(
        200, json=list_json([{"id": "cus_1", "object": "customer"}])
    )
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H).respond(
        200, json=list_json([sub_json("sub_1")])
    )
    respx.get(f"{STRIPE}/customers", headers=LEGACY_H).respond(
        200, json=list_json([{"id": "cus_L", "object": "customer"}])
    )
    respx.get(f"{STRIPE}/subscriptions", headers=LEGACY_H).respond(
        200, json=list_json([sub_json("sub_2", status="past_due")])
    )
    cancel_1 = respx.delete(f"{STRIPE}/subscriptions/sub_1", headers=CURRENT_H).respond(
        200, json=sub_json("sub_1", status="canceled")
    )
    cancel_2 = respx.delete(f"{STRIPE}/subscriptions/sub_2", headers=LEGACY_H).respond(
        200, json=sub_json("sub_2", status="canceled")
    )
    session = FakeSession()

    result = await admin_service.cancel_subscription(
        session, email="jan@x.pl", at_period_end=False, admin_user_id=2
    )

    assert result == {
        "success": True,
        "cancelled": 2,
        "access_until": None,
        "mode": "immediate",
    }
    assert cancel_1.called and cancel_2.called
    reasons = [o for o in session.added if isinstance(o, CancellationReason)]
    assert len(reasons) == 1 and reasons[0].reason == "admin-cancel"
    audits = [o for o in session.added if isinstance(o, AuditLog)]
    assert audits[0].action == "cancel_subscription" and audits[0].admin_user_id == 2


@respx.mock
async def test_admin_cancel_at_period_end(admin_env):
    period_end = int(time.time()) + 5 * 86400
    respx.get(f"{STRIPE}/customers", headers=CURRENT_H).respond(
        200, json=list_json([{"id": "cus_1", "object": "customer"}])
    )
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H).respond(
        200, json=list_json([sub_json("sub_1", period_end=period_end)])
    )
    update = respx.post(f"{STRIPE}/subscriptions/sub_1", headers=CURRENT_H).respond(
        200,
        json={**sub_json("sub_1", period_end=period_end), "cancel_at_period_end": True},
    )
    mock_no_customers(LEGACY_H)

    result = await admin_service.cancel_subscription(
        FakeSession(), email="jan@x.pl", at_period_end=True, admin_user_id=None
    )

    assert result["cancelled"] == 1
    assert result["mode"] == "period_end"
    assert result["access_until"].endswith("Z")
    assert form_of(update.calls.last.request)["cancel_at_period_end"] == "true"


@respx.mock
async def test_admin_cancel_not_found(admin_env):
    mock_no_customers(CURRENT_H)
    mock_no_customers(LEGACY_H)
    with pytest.raises(HTTPException) as exc:
        await admin_service.cancel_subscription(
            FakeSession(), email="nikt@x.pl", at_period_end=True, admin_user_id=None
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "Nie znaleziono aktywnej subskrypcji do anulowania."


# ── GET /admin/cancellations ─────────────────────────────────────────────────


async def test_list_cancellations_shape(admin_env):
    row = CancellationReason(
        id=uuid.uuid4(),
        email="a@b.pl",
        reason="expensive",
        action="cancelled",
        freeze_days=None,
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    session = FakeSession(execute_result=[row])

    result = await admin_service.list_cancellations(session)

    assert result == {
        "rows": [
            {
                "id": str(row.id),
                "email": "a@b.pl",
                "reason": "expensive",
                "action": "cancelled",
                "freezeDays": None,
                "createdAt": "2026-01-02T03:04:05.000Z",
            }
        ]
    }


# ── GET /admin/legacy-audit ──────────────────────────────────────────────────


@respx.mock
async def test_legacy_audit_aggregates_and_recent_failures(admin_env):
    renewal_ts = int(datetime(2027, 1, 15, tzinfo=UTC).timestamp())
    legacy_sub = {
        "id": "sub_z",
        "object": "subscription",
        "status": "active",
        "collection_method": "charge_automatically",
        "cancel_at_period_end": False,
        "customer": {"id": "cus_z", "object": "customer", "email": "old@x.pl"},
        "default_payment_method": {
            "id": "pm_z",
            "object": "payment_method",
            "card": {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2026},
        },
        "items": {
            "object": "list",
            "data": [
                {
                    "id": "si_z",
                    "current_period_end": renewal_ts,
                    "price": {
                        "id": "price_z",
                        "unit_amount": 26900,
                        "recurring": {"interval": "month"},
                    },
                }
            ],
        },
    }
    for status in ("active", "past_due", "unpaid", "trialing", "paused"):
        respx.get(
            f"{STRIPE}/subscriptions", headers=LEGACY_H, params={"status": status}
        ).respond(200, json=list_json([legacy_sub] if status == "active" else []))
    respx.get(f"{STRIPE}/webhook_endpoints", headers=LEGACY_H).respond(
        200,
        json=list_json(
            [
                {
                    "id": "we_1",
                    "object": "webhook_endpoint",
                    "url": "https://api.befreeclub.pro/api/billing/webhooks/stripe/legacy",
                    "status": "enabled",
                },
                {
                    "id": "we_2",
                    "object": "webhook_endpoint",
                    "url": "https://stary.example/webhook",
                    "status": "enabled",
                },
            ]
        ),
    )
    session = FakeSession(execute_result=3)

    result = await admin_service.legacy_audit(session)

    assert result["total_subscriptions"] == 1
    assert result["by_status"] == {"active": 1}
    assert result["by_renewal_month"] == {"2027-01": 1}
    assert result["estimated_mrr_pln"] == 269
    assert result["risks"] == {
        "no_default_payment_method": 0,
        "card_expires_before_renewal": 1,
        "send_invoice_method": 0,
    }
    # DOKONCZONE recent_failures: liczone z webhook_events dla NASZEGO endpointu
    assert result["webhook_endpoints"] == [
        {
            "id": "we_1",
            "url": "https://api.befreeclub.pro/api/billing/webhooks/stripe/legacy",
            "status": "enabled",
            "recent_failures": 3,
        },
        {
            "id": "we_2",
            "url": "https://stary.example/webhook",
            "status": "enabled",
            "recent_failures": 0,
        },
    ]
    [expiring] = result["problem_rows"]["expiring_cards"]
    assert expiring["id"] == "sub_z"
    assert expiring["card_expires_before_renewal"] is True
    assert expiring["amount_pln"] == 269
    assert result["problem_rows"]["no_default_pm"] == []


async def test_legacy_audit_requires_key(admin_env, monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", None)
    stripe_client.reset_clients()
    with pytest.raises(HTTPException) as exc:
        await admin_service.legacy_audit(FakeSession())
    assert exc.value.status_code == 500
    assert exc.value.detail == "STRIPE_LEGACY_SECRET_KEY not set"
