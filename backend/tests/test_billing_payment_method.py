"""Testy portu zmiany karty ([billing-lifecycle], naprawa #2 z PLAN_LANDING):
magic link zamiast "podaj email", OBA konta (legacy odzyskuje zmiane karty),
confirm z retry otwartych faktur i dzialajacym powrotem z 3DS.
"""

import json
import re
import time
from urllib.parse import parse_qs, unquote

import pytest
import respx
from fastapi import HTTPException

from app.core import stripe_client
from app.core.config import settings
from app.modules.billing.services import magic_link
from app.modules.billing.services import payment_method as pm_service

STRIPE = "https://api.stripe.com/v1"
RESEND = "https://api.resend.com/emails"
SECRET = "doi-secret-test"
CURRENT_H = {"Authorization": "Bearer sk_test_current"}
LEGACY_H = {"Authorization": "Bearer sk_test_legacy"}


@pytest.fixture
def pm_env(monkeypatch):
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


def sub_json(sub_id, status="active"):
    return {"id": sub_id, "object": "subscription", "status": status}


def form_of(request) -> dict[str, str]:
    parsed = parse_qs(request.content.decode(), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def make_pm_token(email="jan@x.pl", ttl_ms=3600_000, purpose="update_payment_method"):
    payload = {"email": email, "exp": int(time.time() * 1000) + ttl_ms}
    if purpose is not None:
        payload["purpose"] = purpose
    return magic_link.sign_token(payload, SECRET)


def search_json(data):
    return {"object": "search_result", "data": data, "has_more": False,
            "url": "/v1/customers/search"}


def mock_no_customers(headers):
    respx.get(f"{STRIPE}/customers", headers=headers).respond(200, json=list_json([]))
    # Pusty list -> kod robi fallback customers.search (case-insensitive).
    respx.get(f"{STRIPE}/customers/search", headers=headers).respond(
        200, json=search_json([])
    )


# ── POST /payment-method/request-link ────────────────────────────────────────


async def test_request_link_invalid_email(pm_env):
    with pytest.raises(HTTPException) as exc:
        await pm_service.request_link(email="bez-malpy")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Wpisz prawidłowy adres email."


@respx.mock
async def test_request_link_unknown_email_no_mail(pm_env):
    """Anty-enumeracja: brak konta = brak maila, wynik wewnetrzny found=False
    (publiczny route i tak odpowiada {"ok": true})."""
    mock_no_customers(CURRENT_H)
    mock_no_customers(LEGACY_H)
    resend = respx.post(RESEND).respond(200, json={"id": "x"})

    result = await pm_service.request_link(email="nikt@x.pl")

    assert result == {"found": False, "sent": False, "account": None}
    assert not resend.called


@respx.mock
async def test_request_link_sends_purpose_token(pm_env):
    respx.get(f"{STRIPE}/customers", headers=CURRENT_H).respond(
        200, json=list_json([{"id": "cus_1", "object": "customer"}])
    )
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H).respond(
        200, json=list_json([sub_json("sub_1")])
    )
    resend = respx.post(RESEND).respond(200, json={"id": "email_1"})

    result = await pm_service.request_link(email="  Jan@X.PL ")

    assert result == {"found": True, "sent": True, "account": "current"}
    body = json.loads(resend.calls.last.request.content)
    assert body["to"] == ["jan@x.pl"]
    assert body["subject"] == "Zmiana karty płatniczej Be Free Club"
    match = re.search(
        r'href="https://befreeclub\.pl/aktualizuj-karte\?token=([^"]+)"', body["html"]
    )
    assert match
    payload = magic_link.verify_token(unquote(match.group(1)), SECRET)
    assert payload["email"] == "jan@x.pl"
    assert payload["purpose"] == "update_payment_method"


# ── POST /payment-method/setup-intent (token HMAC, OBA konta) ────────────────


@respx.mock
async def test_setup_intent_finds_legacy_customer(pm_env):
    """NAPRAWA: oryginal patrzyl tylko na current - stary czlonek z suba
    na legacy byl odciety od zmiany karty. Port tworzy SetupIntent na legacy."""
    mock_no_customers(CURRENT_H)
    respx.get(f"{STRIPE}/customers", headers=LEGACY_H).respond(
        200, json=list_json([{"id": "cus_L", "object": "customer"}])
    )
    respx.get(f"{STRIPE}/subscriptions", headers=LEGACY_H).respond(
        200, json=list_json([sub_json("sub_L", status="past_due")])
    )
    create = respx.post(f"{STRIPE}/setup_intents", headers=LEGACY_H).respond(
        200,
        json={"id": "seti_L", "object": "setup_intent", "client_secret": "seti_L_secret"},
    )

    result = await pm_service.create_setup_intent(token=make_pm_token())

    assert result == {
        "clientSecret": "seti_L_secret",
        "setupIntentId": "seti_L",
        "account": "legacy",
    }
    form = form_of(create.calls.last.request)
    assert form["customer"] == "cus_L"
    assert form["usage"] == "off_session"
    assert form["payment_method_types[0]"] == "card"
    assert form["metadata[purpose]"] == "update_payment_method"
    assert form["metadata[customer_email]"] == "jan@x.pl"
    assert create.calls.last.request.headers["idempotency-key"].startswith("pm-update-")


async def test_setup_intent_rejects_invalid_token(pm_env):
    with pytest.raises(HTTPException) as exc:
        await pm_service.create_setup_intent(token="zepsuty.token")
    assert exc.value.status_code == 410
    assert exc.value.detail == (
        "Link wygasł lub jest nieprawidłowy. Wróć na stronę zmiany karty i wyślij nowy."
    )


async def test_setup_intent_rejects_cancellation_token(pm_env):
    """Token anulowania (bez purpose) nie otwiera zmiany karty."""
    with pytest.raises(HTTPException) as exc:
        await pm_service.create_setup_intent(token=make_pm_token(purpose=None))
    assert exc.value.status_code == 410


@respx.mock
async def test_setup_intent_404_messages(pm_env):
    """Komunikaty 404 1:1: brak konta vs konto bez aktywnej suby."""
    mock_no_customers(CURRENT_H)
    mock_no_customers(LEGACY_H)
    with pytest.raises(HTTPException) as exc:
        await pm_service.create_setup_intent(token=make_pm_token())
    assert exc.value.status_code == 404
    assert exc.value.detail == "Nie znaleźliśmy konta z takim adresem email."

    respx.reset()
    respx.get(f"{STRIPE}/customers", headers=CURRENT_H).respond(
        200, json=list_json([{"id": "cus_1", "object": "customer"}])
    )
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H).respond(
        200, json=list_json([sub_json("sub_1", status="canceled")])
    )
    mock_no_customers(LEGACY_H)
    with pytest.raises(HTTPException) as exc:
        await pm_service.create_setup_intent(token=make_pm_token())
    assert exc.value.status_code == 404
    assert exc.value.detail == (
        "Nie znaleźliśmy aktywnej subskrypcji powiązanej z tym adresem email."
    )


# ── POST /payment-method/confirm ─────────────────────────────────────────────


@respx.mock
async def test_confirm_on_legacy_with_invoice_retry(pm_env):
    """SetupIntent zyje na legacy (current zwraca 404 -> SDK rzuca, port
    szuka dalej). Confirm: default PM na customerze + obu subach, retry
    otwartej faktury past_due. Pokrywa tez powrot z 3DS (ten sam endpoint)."""
    respx.get(f"{STRIPE}/setup_intents/seti_9", headers=CURRENT_H).respond(
        404,
        json={"error": {"type": "invalid_request_error", "message": "No such setup_intent"}},
    )
    respx.get(f"{STRIPE}/setup_intents/seti_9", headers=LEGACY_H).respond(
        200,
        json={
            "id": "seti_9",
            "object": "setup_intent",
            "status": "succeeded",
            "customer": "cus_L",
            "payment_method": "pm_9",
        },
    )
    update_customer = respx.post(f"{STRIPE}/customers/cus_L", headers=LEGACY_H).respond(
        200, json={"id": "cus_L", "object": "customer"}
    )
    respx.get(f"{STRIPE}/subscriptions", headers=LEGACY_H).respond(
        200,
        json=list_json([sub_json("sub_a", "active"), sub_json("sub_b", "past_due")]),
    )
    update_a = respx.post(f"{STRIPE}/subscriptions/sub_a", headers=LEGACY_H).respond(
        200, json=sub_json("sub_a")
    )
    update_b = respx.post(f"{STRIPE}/subscriptions/sub_b", headers=LEGACY_H).respond(
        200, json=sub_json("sub_b", "past_due")
    )
    respx.get(f"{STRIPE}/invoices", headers=LEGACY_H).respond(
        200, json=list_json([{"id": "in_1", "object": "invoice", "status": "open"}])
    )
    pay = respx.post(f"{STRIPE}/invoices/in_1/pay", headers=LEGACY_H).respond(
        200, json={"id": "in_1", "object": "invoice", "status": "paid"}
    )

    result = await pm_service.confirm(setup_intent_id="seti_9")

    assert result == {"success": True, "subscriptionsUpdated": 2, "invoicesRetried": 1}
    assert (
        form_of(update_customer.calls.last.request)["invoice_settings[default_payment_method]"]
        == "pm_9"
    )
    assert form_of(update_a.calls.last.request)["default_payment_method"] == "pm_9"
    assert form_of(update_b.calls.last.request)["default_payment_method"] == "pm_9"
    assert form_of(pay.calls.last.request)["payment_method"] == "pm_9"


@respx.mock
async def test_confirm_invoice_pay_failure_swallowed(pm_env):
    """Blad platnosci pojedynczej faktury: logowany i polykany (1:1)."""
    respx.get(f"{STRIPE}/setup_intents/seti_8", headers=CURRENT_H).respond(
        200,
        json={
            "id": "seti_8",
            "object": "setup_intent",
            "status": "succeeded",
            "customer": "cus_1",
            "payment_method": "pm_8",
        },
    )
    respx.post(f"{STRIPE}/customers/cus_1").respond(200, json={"id": "cus_1"})
    respx.get(f"{STRIPE}/subscriptions", headers=CURRENT_H).respond(
        200, json=list_json([sub_json("sub_x", "unpaid")])
    )
    respx.post(f"{STRIPE}/subscriptions/sub_x").respond(200, json=sub_json("sub_x", "unpaid"))
    respx.get(f"{STRIPE}/invoices", headers=CURRENT_H).respond(
        200, json=list_json([{"id": "in_2", "object": "invoice", "status": "open"}])
    )
    respx.post(f"{STRIPE}/invoices/in_2/pay").respond(
        402, json={"error": {"type": "card_error", "message": "Your card was declined."}}
    )

    result = await pm_service.confirm(setup_intent_id="seti_8")

    assert result == {"success": True, "subscriptionsUpdated": 1, "invoicesRetried": 0}


@respx.mock
async def test_confirm_not_succeeded(pm_env):
    respx.get(f"{STRIPE}/setup_intents/seti_7", headers=CURRENT_H).respond(
        200,
        json={"id": "seti_7", "object": "setup_intent", "status": "requires_action"},
    )

    with pytest.raises(HTTPException) as exc:
        await pm_service.confirm(setup_intent_id="seti_7")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Karta nie została potwierdzona (requires_action)."


async def test_confirm_missing_id(pm_env):
    with pytest.raises(HTTPException) as exc:
        await pm_service.confirm(setup_intent_id=None)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Missing setupIntentId"
