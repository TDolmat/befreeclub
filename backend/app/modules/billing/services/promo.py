"""Kody promocyjne: port validate-promo + WSPOLNY cichy lookup
(confirm-subscription i create-klarna-checkout mialy wlasne kopie).

Semantyka 1:1 ze specem billing-checkout.md sekcja 5:
- lookup ZAWSZE po tekscie kodu na koncie current ("never trust client" -
  promotionCodeId od klienta jest celowo ignorowany),
- normalizacja: trim().toUpperCase(),
- wlasny check expires_at oprocz filtra active=true,
- cichy fallback: kod nieznaleziony/wygasly/blad Stripe = zakup idzie BEZ
  rabatu, bez informowania usera.
"""

import time
from typing import Any

from app.core.logging import create_logger
from app.core.stripe_client import StripeAccount, get_client

log = create_logger("billing.promo")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    """Bezpieczny odczyt pola StripeObject (v15 nie jest dict, brak .get())."""
    if obj is None:
        return default
    try:
        return obj[key]
    except (KeyError, TypeError):
        return default


async def lookup_active_promotion_code(code: str) -> Any | None:
    """Cichy server-side lookup kodu (confirm-subscription / create-klarna 1:1).

    Zwraca obiekt promotion code albo None (nieznaleziony / wygasly / blad).
    """
    normalized = code.strip().upper()
    try:
        client = get_client(StripeAccount.CURRENT)
        result = await client.v1.promotion_codes.list_async(
            params={"code": normalized, "active": True, "limit": 1}
        )
        data = result.data
        if not data:
            log.info(f"Promo not found, skipping: {normalized}")
            return None
        promo = data[0]
        expires_at = _obj_get(promo, "expires_at")
        # notExpired = !expires_at || expires_at * 1000 > Date.now() (1:1)
        if expires_at and expires_at * 1000 <= _now_ms():
            log.info(f"Promo expired, skipping: {normalized}")
            return None
        log.info(f"Applying promo: {normalized} {promo.id}")
        return promo
    except Exception as err:
        log.error(f"Promo lookup failed: {err}")
        return None


async def validate_code(code: str | None) -> dict:
    """Port validate-promo: odpowiedz ZAWSZE 200 z {"valid": ...} (ksztalt 1:1).

    To odpowiedz biznesowa, nie blad - dlatego wyjatki tez wracaja jako
    {"valid": false, "reason": "error", "message": ...}.
    """
    try:
        if not code or not isinstance(code, str):
            raise ValueError("Missing or invalid code")

        normalized = code.strip().upper()
        client = get_client(StripeAccount.CURRENT)
        result = await client.v1.promotion_codes.list_async(
            params={"code": normalized, "active": True, "limit": 1}
        )
        if not result.data:
            return {"valid": False, "reason": "not_found"}

        promo = result.data[0]
        expires_at = _obj_get(promo, "expires_at")
        if expires_at and expires_at * 1000 < _now_ms():
            return {"valid": False, "reason": "expired"}

        coupon = _obj_get(promo, "coupon")
        return {
            "valid": True,
            "code": normalized,
            "promotionCodeId": promo.id,
            "discountPercent": _obj_get(coupon, "percent_off"),
            "discountAmount": _obj_get(coupon, "amount_off"),
            "currency": _obj_get(coupon, "currency"),
            "duration": _obj_get(coupon, "duration"),
            "durationInMonths": _obj_get(coupon, "duration_in_months"),
            "expiresAt": expires_at,
        }
    except Exception as err:
        log.error(f"validate-promo error: {err}")
        return {"valid": False, "reason": "error", "message": str(err)}
