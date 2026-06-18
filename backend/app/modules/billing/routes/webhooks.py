"""Webhooki Stripe OBU kont (port stripe-webhook + NOWY endpoint legacy).

Montowane pod /api/billing/webhooks (publiczne, bez require_auth - auth =
weryfikacja podpisu Stripe w handlerze, sekret per konto):
  POST /stripe/current   STRIPE_WEBHOOK_SECRET
  POST /stripe/legacy    STRIPE_LEGACY_WEBHOOK_SECRET (naprawa #4: nieudane
                         odnowienia i refundy starych czlonkow w koncu
                         wywoluja reakcje)

Przebieg (port-kontrakt-2.md wiersz 8):
1. Podpis: stripe-signature + lokalna kryptografia HMAC (zly podpis -> 400).
2. Zapis do billing.webhook_events PRZED przetworzeniem; UNIQUE event_id =
   idempotencja (INSERT ... ON CONFLICT DO NOTHING; duplikat -> 200 bez akcji).
   Pelny payload w JSONB - panel "komu cos nie przeszlo i czemu" czyta
   z tej tabeli (np. powod nieudanej platnosci), nie ze Stripe na zywo.
3. Dispatch do services/webhook_handlers.process_event.
4. Sukces -> processed_at; wyjatek -> wpis `error`, processed_at NULL,
   response MIMO TO 200 (Stripe nie retry'uje, historia zostaje w panelu).
"""

import json
from datetime import UTC, datetime
from typing import Any

import stripe
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.logging import create_logger
from app.core.stripe_client import StripeAccount, webhook_secret_for
from app.modules.billing.models import WebhookEvent
from app.modules.billing.services import webhook_handlers

log = create_logger("billing.webhook")

router = APIRouter()

SIGNATURE_TOLERANCE_SECONDS = 300  # default Stripe SDK


@router.post("/stripe/current")
async def stripe_webhook_current(
    request: Request, db: AsyncSession = Depends(get_session)
) -> JSONResponse:
    return await _handle_webhook(request, db, StripeAccount.CURRENT)


@router.post("/stripe/legacy")
async def stripe_webhook_legacy(
    request: Request, db: AsyncSession = Depends(get_session)
) -> JSONResponse:
    return await _handle_webhook(request, db, StripeAccount.LEGACY)


async def _handle_webhook(
    request: Request, db: AsyncSession, account: StripeAccount
) -> JSONResponse:
    signature = request.headers.get("stripe-signature")
    if not signature:
        return JSONResponse({"error": "Missing stripe-signature"}, status_code=400)

    secret = webhook_secret_for(account)
    if not secret:
        log.error(f"Webhook secret for account {account.value} is not configured")
        return JSONResponse({"error": "Webhook not configured"}, status_code=500)

    body = await request.body()
    try:
        # Ta sama lokalna kryptografia co stripe.Webhook.construct_event;
        # payload dalej parsujemy sami, zeby miec czysty dict pod JSONB.
        stripe.WebhookSignature.verify_header(
            body.decode("utf-8"), signature, secret, SIGNATURE_TOLERANCE_SECONDS
        )
    except stripe.SignatureVerificationError as err:
        log.error(f"Signature verification failed ({account.value}): {err}")
        return JSONResponse({"error": "Invalid signature"}, status_code=400)

    try:
        event: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid payload"}, status_code=400)
    event_id = event.get("id")
    event_type = event.get("type")
    if not isinstance(event, dict) or not event_id or not event_type:
        return JSONResponse({"error": "Invalid payload"}, status_code=400)

    log.info(f"Received event: {event_type} ({event_id}, {account.value})")

    # Zapis PRZED przetworzeniem; UNIQUE event_id = idempotencja (Stripe
    # potrafi dostarczyc event 2x - duplikat konczy sie tutaj, bez akcji).
    row_id = (
        await db.execute(
            pg_insert(WebhookEvent)
            .values(
                stripe_account=account.value,
                event_id=str(event_id),
                type=str(event_type),
                payload=event,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(WebhookEvent.id)
        )
    ).scalar_one_or_none()
    await db.commit()
    if row_id is None:
        log.info(f"Duplicate event {event_id}, skipping")
        return JSONResponse({"received": True}, status_code=200)

    try:
        await webhook_handlers.process_event(db, account, event)
    except Exception as err:
        log.error(f"Event {event_id} ({event_type}) handling failed", str(err))
        await db.rollback()
        await db.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id == row_id)
            .values(error=str(err)[:2000])
        )
        await db.commit()
        # 200 mimo bledu: event zapisany (processed_at NULL + error), panel
        # widzi co nie przeszlo; Stripe nie bombarduje retry'ami.
        return JSONResponse({"received": True}, status_code=200)

    await db.execute(
        update(WebhookEvent)
        .where(WebhookEvent.id == row_id)
        .values(processed_at=datetime.now(UTC))
    )
    await db.commit()
    return JSONResponse({"received": True}, status_code=200)
