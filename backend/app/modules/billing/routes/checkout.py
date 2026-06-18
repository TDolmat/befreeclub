"""Publiczne endpointy checkoutu (port create-checkout, confirm-subscription,
create-klarna-checkout, confirm-klarna-checkout). Montowane pod
/api/billing/checkout w main.py.

Rate limit (kontrakt 1.4): create'y (setup-intent, klarna) przez limiter
checkoutowy 30/15min; confirmy bez limitu. client_ip/user-agent do atrybucji
bierze BACKEND z requestu, nie front (kontrakt 5.1).
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.modules.admin.services.rate_limit import client_ip
from app.modules.billing.schemas import (
    CheckoutConfirmIn,
    CheckoutSetupIntentIn,
    KlarnaCheckoutIn,
    KlarnaConfirmIn,
)
from app.modules.billing.services import checkout as checkout_service
from app.modules.billing.services import rate_limit as checkout_rate_limit

router = APIRouter()


@router.post("/setup-intent")
async def create_setup_intent(
    body: CheckoutSetupIntentIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    checkout_rate_limit.enforce(request, "checkout-setup-intent")
    return await checkout_service.create_setup_intent(
        session,
        plan_id=body.plan_id,
        attribution=body.attribution,
        client_ip=client_ip(request),
        client_ua=request.headers.get("user-agent"),
    )


@router.post("/confirm")
async def confirm_subscription(
    body: CheckoutConfirmIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await checkout_service.confirm_subscription(
        session,
        setup_intent_id=body.setup_intent_id,
        plan_id=body.plan_id,
        email=body.email,
        want_invoice=body.want_invoice,
        nip=body.nip,
        promo_code=body.promo_code,
        attribution=body.attribution,
        client_ip=client_ip(request),
        client_ua=request.headers.get("user-agent"),
    )


@router.post("/klarna")
async def create_klarna_checkout(
    body: KlarnaCheckoutIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    checkout_rate_limit.enforce(request, "checkout-klarna")
    return await checkout_service.create_klarna_session(
        session,
        plan_id=body.plan_id,
        email=body.email,
        promo_code=body.promo_code,
        attribution=body.attribution,
        origin=request.headers.get("origin"),
        client_ip=client_ip(request),
        client_ua=request.headers.get("user-agent"),
    )


@router.post("/klarna/confirm")
async def confirm_klarna_checkout(
    body: KlarnaConfirmIn,
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await checkout_service.confirm_klarna(session, session_id=body.session_id)
