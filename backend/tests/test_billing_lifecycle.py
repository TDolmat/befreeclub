"""Testy portu anulowania ([billing-lifecycle]): magic link HMAC,
request/confirm-cancellation, wspolne subscriptions.has_live_access.

Stripe mockowany respx (SDK przechodzi przez httpx; konta rozrozniane
naglowkiem Authorization), Resend przez respx, DB przez FakeSession.
"""

import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote

import httpx
import pytest
import respx
from fastapi import HTTPException

from app.core import stripe_client
from app.core.config import settings
from app.modules.admin.services import rate_limit as mail_rate_limit
from app.modules.billing.models import CancellationReason
from app.modules.billing.routes import cancellation as cancellation_routes
from app.modules.billing.services import cancellation as cancellation_service
from app.modules.billing.services import magic_link
from app.modules.billing.services import subscriptions as subscriptions_service

STRIPE = "https://api.stripe.com/v1"
RESEND = "https://api.resend.com/emails"
SECRET = "doi-secret-test"
CURRENT_H = {"Authorization": "Bearer sk_test_current"}
LEGACY_H = {"Authorization": "Bearer sk_test_legacy"}


class FakeSession:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


@pytest.fixture
def lifecycle_env(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_current")
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", "sk_test_legacy")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(settings, "CANCELLATION_DOI_SECRET", SECRET)
    monkeypatch.setattr(settings, "CANCELLATION_FROM_EMAIL", None)
    monkeypatch.setattr(settings, "FRONTEND_URL", None)
    stripe_client.reset_clients()
    magic_link.reset_used()
    yield
    stripe_client.reset_clients()
    magic_link.reset_used()


def list_json(data):
    return {"object": "list", "data": data, "has_more": False}


def sub_json(sub_id, status="active", period_end=None, cancel_at_period_end=False):
    item = {"id": f"si_{sub_id}", "object": "subscription_item"}
    if period_end is not None:
        item["current_period_end"] = period_end
    return {
        "id": sub_id,
        "object": "subscription",
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "items": {"object": "list", "data": [item]},
    }


def make_token(email="jan@x.pl", ttl_ms=3600_000, **extra):
    payload = {"email": email, "exp": int(time.time() * 1000) + ttl_ms, **extra}
    return magic_link.sign_token(payload, SECRET)


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


# ── magic link (sign/verify/rejestr) ─────────────────────────────────────────


def test_token_roundtrip_and_tamper():
    token = magic_link.sign_token({"email": "a@b.pl", "exp": int(time.time() * 1000) + 1000}, SECRET)
    payload = magic_link.verify_token(token, SECRET)
    assert payload is not None and payload["email"] == "a@b.pl"
    # zly sekret / przerobiony payload / zly format -> None
    assert magic_link.verify_token(token, "inny-sekret") is None
    data, sig = token.split(".")
    assert magic_link.verify_token(f"{data}x.{sig}", SECRET) is None
    assert magic_link.verify_token("nie-token", SECRET) is None


def test_token_expired():
    token = magic_link.sign_token({"email": "a@b.pl", "exp": int(time.time() * 1000) - 1}, SECRET)
    assert magic_link.verify_token(token, SECRET) is None


def test_used_registry():
    magic_link.reset_used()
    token = make_token()
    assert not magic_link.is_used(token)
    magic_link.mark_used(token, exp_ms=time.time() * 1000 + 1000)
    assert magic_link.is_used(token)
    magic_link.reset_used()
    assert not magic_link.is_used(token)


def test_claim_is_atomic_and_releasable():
    """Review 2.1: claim atomowo zuzywa token (drugi claim odpada),
    release oddaje go na sciezkach bez skutku (404/blad Stripe)."""
    magic_link.reset_used()
    token = make_token()
    exp = time.time() * 1000 + 1000
    assert magic_link.claim(token, exp_ms=exp) is True
    assert magic_link.claim(token, exp_ms=exp) is False  # juz zuzyty
    magic_link.release(token)
    assert magic_link.claim(token, exp_ms=exp) is True
    magic_link.reset_used()


# ── POST /cancellation/request (port request-cancellation) ───────────────────


@respx.mock
async def test_request_cancellation_sends_magic_link(lifecycle_env):
    respx.get(f"{STRIPE}/customers", headers=CURRENT_H).respond(
        200, json=list_json([{"id": "cus_1", "object": "customer", "email": "jan@x.pl"}])
    )
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H).respond(
        200, json=list_json([sub_json("sub_1")])
    )
    mock_no_customers(LEGACY_H)
    resend = respx.post(RESEND).respond(200, json={"id": "email_1"})

    result = await cancellation_service.request_cancellation(
        email="  Jan@X.PL ", reason="expensive"
    )

    assert result == {"success": True}
    sent = resend.calls.last.request
    import json as json_mod

    body = json_mod.loads(sent.content)
    assert body["to"] == ["jan@x.pl"]
    assert body["subject"] == "Potwierdź anulowanie subskrypcji Be Free Club"
    assert body["from"] == "Be Free Club <noreply@befreeclub.pl>"
    assert body["reply_to"] == "kontakt@befreeclub.pl"
    # link do /anuluj/potwierdz z waznym tokenem; reason w payloadzie tokenu
    match = re.search(r'href="https://befreeclub\.pl/anuluj/potwierdz\?token=([^"]+)"', body["html"])
    assert match
    payload = magic_link.verify_token(unquote(match.group(1)), SECRET)
    assert payload is not None
    assert payload["email"] == "jan@x.pl"
    assert payload["reason"] == "expensive"


@respx.mock
async def test_request_cancellation_long_reason_dropped(lifecycle_env):
    """Powod >60 znakow jest po cichu gubiony - 1:1 z oryginalem."""
    respx.get(f"{STRIPE}/customers", headers=CURRENT_H).respond(
        200, json=list_json([{"id": "cus_1", "object": "customer"}])
    )
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H).respond(
        200, json=list_json([sub_json("sub_1")])
    )
    mock_no_customers(LEGACY_H)
    resend = respx.post(RESEND).respond(200, json={"id": "email_1"})

    await cancellation_service.request_cancellation(email="jan@x.pl", reason="x" * 61)

    html = resend.calls.last.request.content.decode()
    match = re.search(r"token=([^\\\"]+)", html)
    payload = magic_link.verify_token(unquote(match.group(1)), SECRET)
    assert "reason" not in payload


@respx.mock
async def test_request_cancellation_not_found(lifecycle_env):
    mock_no_customers(CURRENT_H)
    mock_no_customers(LEGACY_H)
    with pytest.raises(HTTPException) as exc:
        await cancellation_service.request_cancellation(email="nikt@x.pl", reason=None)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Nie znaleziono aktywnej subskrypcji dla tego adresu email."


async def test_request_cancellation_email_required(lifecycle_env):
    with pytest.raises(HTTPException) as exc:
        await cancellation_service.request_cancellation(email=None, reason=None)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Email jest wymagany"


# ── POST /cancellation/confirm (port confirm-cancellation) ───────────────────


def mock_cancellable_sub_on_current(period_end: int):
    """Mock: current ma 1 anulowalna sube, legacy pusty. Zwraca route update."""
    respx.get(f"{STRIPE}/customers", headers=CURRENT_H).respond(
        200, json=list_json([{"id": "cus_1", "object": "customer"}])
    )
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H).respond(
        200, json=list_json([sub_json("sub_1", period_end=period_end)])
    )
    mock_no_customers(LEGACY_H)
    return respx.post(f"{STRIPE}/subscriptions/sub_1", headers=CURRENT_H).respond(
        200,
        json=sub_json("sub_1", period_end=period_end, cancel_at_period_end=True),
    )


@respx.mock
async def test_confirm_cancellation_happy_path(lifecycle_env):
    period_end = int(time.time()) + 10 * 86400
    update = mock_cancellable_sub_on_current(period_end)
    session = FakeSession()
    token = make_token(reason="no-time")

    result = await cancellation_service.confirm_cancellation(session, token=token)

    assert result["success"] is True
    assert result["cancelled"] == 1
    assert result["access_until"].endswith("Z")
    assert form_of(update.calls.last.request)["cancel_at_period_end"] == "true"
    # wpis audytowy ZAWSZE (kontrakt #11)
    [row] = session.added
    assert isinstance(row, CancellationReason)
    assert row.reason == "no-time"
    assert row.action == "cancelled"
    assert session.commits == 1


@respx.mock
async def test_confirm_cancellation_token_single_use(lifecycle_env):
    """Naprawa 'tokenu wielokrotnego uzytku': drugi confirm tym samym
    tokenem odpada z 410, bez requestow do Stripe."""
    period_end = int(time.time()) + 86400
    mock_cancellable_sub_on_current(period_end)
    token = make_token()

    await cancellation_service.confirm_cancellation(FakeSession(), token=token)
    respx.reset()  # zadnych mockow - drugi confirm nie ma prawa wyjsc w swiat

    with pytest.raises(HTTPException) as exc:
        await cancellation_service.confirm_cancellation(FakeSession(), token=token)
    assert exc.value.status_code == 410
    assert exc.value.detail == (
        "Link wygasł lub jest nieprawidłowy. Wróć na stronę anulowania i wyślij nowy."
    )


async def test_confirm_cancellation_expired_token(lifecycle_env):
    with pytest.raises(HTTPException) as exc:
        await cancellation_service.confirm_cancellation(
            FakeSession(), token=make_token(ttl_ms=-1000)
        )
    assert exc.value.status_code == 410


async def test_confirm_cancellation_rejects_payment_method_token(lifecycle_env):
    """Token zmiany karty (purpose w payloadzie) nie anuluje subskrypcji,
    mimo ze sekret jest wspolny."""
    token = make_token(purpose="update_payment_method")
    with pytest.raises(HTTPException) as exc:
        await cancellation_service.confirm_cancellation(FakeSession(), token=token)
    assert exc.value.status_code == 410


async def test_confirm_cancellation_missing_token(lifecycle_env):
    with pytest.raises(HTTPException) as exc:
        await cancellation_service.confirm_cancellation(FakeSession(), token=None)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Brak tokenu"


@respx.mock
async def test_confirm_cancellation_no_reason_writes_not_given(lifecycle_env):
    mock_cancellable_sub_on_current(int(time.time()) + 86400)
    session = FakeSession()

    await cancellation_service.confirm_cancellation(session, token=make_token())

    [row] = session.added
    assert row.reason == "not-given"


@respx.mock
async def test_confirm_cancellation_zero_subs_keeps_token(lifecycle_env):
    """0 anulowanych = 404, token NIE zostaje zuzyty (mozna powtorzyc)."""
    mock_no_customers(CURRENT_H)
    mock_no_customers(LEGACY_H)
    token = make_token()
    with pytest.raises(HTTPException) as exc:
        await cancellation_service.confirm_cancellation(FakeSession(), token=token)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Nie znaleziono aktywnej subskrypcji do anulowania."
    assert not magic_link.is_used(token)


@respx.mock
async def test_confirm_skips_already_scheduled_but_counts(lifecycle_env):
    """Suba z cancel_at_period_end juz ustawionym: liczona, bez update (1:1)."""
    respx.get(f"{STRIPE}/customers", headers=CURRENT_H).respond(
        200, json=list_json([{"id": "cus_1", "object": "customer"}])
    )
    period_end = int(time.time()) + 86400
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H).respond(
        200,
        json=list_json([sub_json("sub_1", period_end=period_end, cancel_at_period_end=True)]),
    )
    mock_no_customers(LEGACY_H)
    update = respx.post(f"{STRIPE}/subscriptions/sub_1").respond(200, json={})

    result = await cancellation_service.confirm_cancellation(FakeSession(), token=make_token())

    assert result["cancelled"] == 1
    assert not update.called


# ── subscriptions.has_live_access (logika KEEP z circle-cleanup) ─────────────


@respx.mock
async def test_has_live_access_canceled_future_period(lifecycle_env):
    respx.get(f"{STRIPE}/customers", headers=CURRENT_H).respond(
        200, json=list_json([{"id": "cus_1", "object": "customer"}])
    )
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H).respond(
        200,
        json=list_json(
            [sub_json("sub_1", status="canceled", period_end=int(time.time()) + 3600)]
        ),
    )
    mock_no_customers(LEGACY_H)
    assert await subscriptions_service.has_live_access("jan@x.pl") is True


@respx.mock
async def test_has_live_access_canceled_past_period(lifecycle_env):
    respx.get(f"{STRIPE}/customers", headers=CURRENT_H).respond(
        200, json=list_json([{"id": "cus_1", "object": "customer"}])
    )
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H).respond(
        200,
        json=list_json(
            [sub_json("sub_1", status="canceled", period_end=int(time.time()) - 3600)]
        ),
    )
    mock_no_customers(LEGACY_H)
    assert await subscriptions_service.has_live_access("jan@x.pl") is False


@respx.mock
async def test_has_live_access_keep_status_on_legacy(lifecycle_env):
    mock_no_customers(CURRENT_H)
    respx.get(f"{STRIPE}/customers", headers=LEGACY_H).respond(
        200, json=list_json([{"id": "cus_L", "object": "customer"}])
    )
    respx.get(f"{STRIPE}/subscriptions", headers=LEGACY_H).respond(
        200, json=list_json([sub_json("sub_L", status="past_due")])
    )
    assert await subscriptions_service.has_live_access("jan@x.pl") is True


# ── rate limit endpointu mailowego (kontrakt 1.4: 5 prob / 15 min) ───────────


async def test_request_route_rate_limited(lifecycle_env, monkeypatch):
    mail_rate_limit._buckets.clear()
    calls = []

    async def fake_request(*, email, reason):
        calls.append(email)
        return {"success": True}

    monkeypatch.setattr(
        cancellation_routes.cancellation_service, "request_cancellation", fake_request
    )
    request = SimpleNamespace(headers=httpx.Headers({"x-real-ip": "7.7.7.7"}))
    body = SimpleNamespace(email="jan@x.pl", reason=None)
    try:
        for _ in range(5):
            await cancellation_routes.request_cancellation(body, request)
        with pytest.raises(HTTPException) as exc:
            await cancellation_routes.request_cancellation(body, request)
        assert exc.value.status_code == 429
        assert exc.value.detail == "Zbyt wiele prób. Spróbuj ponownie później."
        assert len(calls) == 5
    finally:
        mail_rate_limit._buckets.clear()
