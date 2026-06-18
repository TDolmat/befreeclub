"""Testy portu validate-promo + wspolnego cichego lookupu ([billing-checkout])."""

import time

import pytest
import respx

from app.core import stripe_client
from app.core.config import settings
from app.modules.billing.services import promo as promo_service

STRIPE = "https://api.stripe.com/v1"


@pytest.fixture
def stripe_current(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_current")
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", None)
    stripe_client.reset_clients()
    yield
    stripe_client.reset_clients()


def promo_json(code="LATO", percent_off=20.0, expires_at=None):
    return {
        "object": "list",
        "has_more": False,
        "data": [
            {
                "id": "promo_1",
                "object": "promotion_code",
                "code": code,
                "active": True,
                "expires_at": expires_at,
                "coupon": {
                    "id": "coupon_1",
                    "object": "coupon",
                    "percent_off": percent_off,
                    "amount_off": None,
                    "currency": None,
                    "duration": "once",
                    "duration_in_months": None,
                },
            }
        ],
    }


@respx.mock
async def test_validate_promo_ok_full_shape(stripe_current):
    route = respx.get(f"{STRIPE}/promotion_codes").respond(200, json=promo_json())

    result = await promo_service.validate_code("  lato ")

    # ksztalt 1:1 z validate-promo (zawsze 200, pelen zestaw pol)
    assert result == {
        "valid": True,
        "code": "LATO",
        "promotionCodeId": "promo_1",
        "discountPercent": 20.0,
        "discountAmount": None,
        "currency": None,
        "duration": "once",
        "durationInMonths": None,
        "expiresAt": None,
    }
    # lookup po znormalizowanym (trim+UPPER) tekscie kodu, tylko aktywne
    params = dict(route.calls.last.request.url.params)
    assert params["code"] == "LATO"
    assert params["active"] == "true"
    assert params["limit"] == "1"


@respx.mock
async def test_validate_promo_not_found(stripe_current):
    respx.get(f"{STRIPE}/promotion_codes").respond(
        200, json={"object": "list", "data": [], "has_more": False}
    )
    assert await promo_service.validate_code("NIEMA") == {"valid": False, "reason": "not_found"}


@respx.mock
async def test_validate_promo_expired(stripe_current):
    past = int(time.time()) - 3600
    respx.get(f"{STRIPE}/promotion_codes").respond(200, json=promo_json(expires_at=past))
    assert await promo_service.validate_code("LATO") == {"valid": False, "reason": "expired"}


async def test_validate_promo_missing_code_is_error_response(stripe_current):
    result = await promo_service.validate_code(None)
    assert result["valid"] is False
    assert result["reason"] == "error"
    assert result["message"] == "Missing or invalid code"


@respx.mock
async def test_validate_promo_stripe_error_returns_error_reason(stripe_current):
    respx.get(f"{STRIPE}/promotion_codes").respond(
        500, json={"error": {"message": "boom", "type": "api_error"}}
    )
    result = await promo_service.validate_code("LATO")
    assert result["valid"] is False
    assert result["reason"] == "error"


# ── wspolny cichy lookup (confirm-subscription / create-klarna 1:1) ──────────


@respx.mock
async def test_lookup_returns_promo_when_active(stripe_current):
    respx.get(f"{STRIPE}/promotion_codes").respond(200, json=promo_json())
    promo = await promo_service.lookup_active_promotion_code(" lato ")
    assert promo is not None
    assert promo.id == "promo_1"


@respx.mock
async def test_lookup_silent_none_when_expired(stripe_current):
    past = int(time.time()) - 3600
    respx.get(f"{STRIPE}/promotion_codes").respond(200, json=promo_json(expires_at=past))
    assert await promo_service.lookup_active_promotion_code("LATO") is None


@respx.mock
async def test_lookup_silent_none_on_stripe_error(stripe_current):
    respx.get(f"{STRIPE}/promotion_codes").respond(
        500, json={"error": {"message": "boom", "type": "api_error"}}
    )
    assert await promo_service.lookup_active_promotion_code("LATO") is None
