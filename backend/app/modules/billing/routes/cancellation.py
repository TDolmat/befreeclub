"""Publiczne endpointy anulowania (port request-cancellation,
confirm-cancellation). Montowane pod /api/billing/cancellation w main.py.

- POST /request: rate limit mailowy (limiter fazy 1: 5 prob / 15 min ->
  lock 1 h, kontrakt 1.4), wysyla magic link HMAC.
- POST /confirm: tylko jawny POST z tokenem w BODY (nigdy GET/query) -
  skanery linkow w poczcie nie anuluja subskrypcji za usera; front
  potwierdza KLIKNIECIEM, nie useEffectem (naprawa, kontrakt 1.3).
  Token jednorazowy.

pause-subscription (self-service po 6-cyfrowym kodzie) NIE jest portowany -
martwy flow (nic nie generowalo kodow); pauza zyje jako akcja admina.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.modules.admin.services import rate_limit as mail_rate_limit
from app.modules.admin.services.rate_limit import client_ip
from app.modules.billing.schemas import CancellationConfirmIn, CancellationRequestIn
from app.modules.billing.services import cancellation as cancellation_service

router = APIRouter()

RATE_LIMIT_MESSAGE = "Zbyt wiele prób. Spróbuj ponownie później."


def _enforce_mail_rate_limit(request: Request, endpoint: str) -> None:
    """Wzorzec z kontraktu 1.4: kazdy request zuzywa probe, lock -> 429."""
    key = f"{endpoint}|{client_ip(request)}"
    if mail_rate_limit.is_locked(key)["locked"]:
        raise HTTPException(429, RATE_LIMIT_MESSAGE)
    mail_rate_limit.record_failure(key)


@router.post("/request")
async def request_cancellation(body: CancellationRequestIn, request: Request) -> dict:
    _enforce_mail_rate_limit(request, "cancellation-request")
    return await cancellation_service.request_cancellation(email=body.email, reason=body.reason)


@router.post("/confirm")
async def confirm_cancellation(
    body: CancellationConfirmIn,
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await cancellation_service.confirm_cancellation(session, token=body.token)
