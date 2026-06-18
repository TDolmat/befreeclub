"""Publiczne endpointy zmiany karty (port update-payment-method z naprawa #2).
Montowane pod /api/billing/payment-method w main.py.

Sciezki KANONICZNE wg port-kontrakt-2.md: /request-link, /setup-intent,
/confirm (aliasy /request i /session skasowane w review 2.1, zanim front
2.4 zdazyl sie ich nauczyc).

- POST /request-link: rate limit mailowy; ZAWSZE {"ok": true}
  (anty-enumeracja - swiadoma zmiana vs 404 oryginalu).
- POST /setup-intent: wymaga waznego tokenu HMAC z maila; szuka klienta
  na OBU kontach (naprawa: legacy odzyskuje zmiane karty).
- POST /confirm: po stripe.confirmSetup na froncie ORAZ po powrocie z 3DS
  (front trzyma token w return_url, setupIntentId bierze z parametrow
  doklejonych przez Stripe).
"""

from fastapi import APIRouter, HTTPException, Request

from app.modules.admin.services import rate_limit as mail_rate_limit
from app.modules.admin.services.rate_limit import client_ip
from app.modules.billing.schemas import (
    PaymentMethodConfirmIn,
    PaymentMethodRequestLinkIn,
    PaymentMethodSetupIntentIn,
)
from app.modules.billing.services import payment_method as payment_method_service

router = APIRouter()

RATE_LIMIT_MESSAGE = "Zbyt wiele prób. Spróbuj ponownie później."


def _enforce_mail_rate_limit(request: Request, endpoint: str) -> None:
    key = f"{endpoint}|{client_ip(request)}"
    if mail_rate_limit.is_locked(key)["locked"]:
        raise HTTPException(429, RATE_LIMIT_MESSAGE)
    mail_rate_limit.record_failure(key)


@router.post("/request-link")
async def request_link(body: PaymentMethodRequestLinkIn, request: Request) -> dict:
    _enforce_mail_rate_limit(request, "payment-method-request-link")
    await payment_method_service.request_link(email=body.email)
    # Anty-enumeracja: brak konta/suby i sukces wygladaja identycznie.
    return {"ok": True}


@router.post("/setup-intent")
async def create_setup_intent(body: PaymentMethodSetupIntentIn) -> dict:
    return await payment_method_service.create_setup_intent(token=body.token)


@router.post("/confirm")
async def confirm(body: PaymentMethodConfirmIn) -> dict:
    return await payment_method_service.confirm(setup_intent_id=body.setup_intent_id)
