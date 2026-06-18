"""Ebook (port flow A, webhook-first). Montowane pod /api/billing/ebook (publiczne).

  POST /payment-intent   port create-ebook-payment-intent: kwota z billing.plans
                         (slug `ebook`), Idempotency-Key, attribution.store,
                         BEZ wiersza pending z placeholderowym emailem (dlug #11)
  POST /confirm          port confirm-ebook-purchase: przyspieszacz UX, weryfikuje
                         status PI w Stripe i wola wspolny fulfill_ebook_order
                         (ten sam, ktory [billing-webhook] wola z payment_intent.succeeded)
  GET  /download?token=  port download-ebook: streaming PDF z EBOOK_FILE_PATH,
                         atomowy licznik pobran, revoked_at po refundzie

create-ebook-checkout NIE jest portowany (martwy flow B).
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.schemas import dump
from app.core.stripe_client import (
    StripeAccount,
    get_client,
    new_idempotency_key,
    request_options,
)
from app.modules.admin.services.rate_limit import client_ip
from app.modules.billing.schemas import (
    EbookConfirmIn,
    EbookConfirmOut,
    EbookPaymentIntentIn,
    EbookPaymentIntentOut,
)
from app.modules.billing.services import attribution, plans
from app.modules.billing.services import rate_limit as checkout_rate_limit
from app.modules.billing.services.ebook import (
    EBOOK_DESCRIPTION,
    EBOOK_PLAN_SLUG,
    DownloadTokenError,
    EbookFulfillmentError,
    consume_download_token,
    fulfill_ebook_order,
)

router = APIRouter()


def _enforce_rate_limit(request: Request) -> None:
    """RL-checkout wg port-kontrakt-2.md 1.4 - kazdy request zuzywa probe."""
    key = f"ebook-payment-intent|{client_ip(request)}"
    if checkout_rate_limit.is_locked(key)["locked"]:
        raise HTTPException(429, "Zbyt wiele prób. Spróbuj ponownie później.")
    checkout_rate_limit.record_failure(key)


@router.post("/payment-intent")
async def create_payment_intent(
    payload: EbookPaymentIntentIn,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict:
    _enforce_rate_limit(request)

    plan = await plans.get_by_slug(EBOOK_PLAN_SLUG, session=db)
    if plan is None or not plan.active:
        raise HTTPException(409, "Ebook jest obecnie niedostępny.")

    # Metadata 1:1 ({product: "ebook"}) + utm/fbclid/fbp/fbc (sekcja 5.1
    # kontraktu: webhook ma atrybucje pod reka bez JOINa) + dane fakturowe
    # (webhook-first fulfillment dostaje je bez powrotu przegladarki).
    metadata: dict[str, str] = {"product": "ebook"}
    a = payload.attribution
    if a is not None:
        for key, value in (
            ("utm_source", a.utm_source),
            ("utm_medium", a.utm_medium),
            ("utm_campaign", a.utm_campaign),
            ("utm_term", a.utm_term),
            ("utm_content", a.utm_content),
            ("fbclid", a.fbclid),
            ("fbp", a.fbp),
            ("fbc", a.fbc),
        ):
            if value:
                metadata[key] = value
    if payload.want_invoice:
        metadata["wants_invoice"] = "true"
        if payload.nip:
            metadata["nip"] = payload.nip
        if payload.invoice_name:
            metadata["invoice_name"] = payload.invoice_name

    client = get_client(StripeAccount.CURRENT)
    intent = await client.v1.payment_intents.create_async(
        params={
            "amount": plan.amount_pln,
            "currency": "pln",
            "payment_method_types": ["card", "blik"],
            "description": EBOOK_DESCRIPTION,
            "metadata": metadata,
        },
        options=request_options(idempotency_key=new_idempotency_key("ebook-pi")),
    )

    # Atrybucja przy KAZDYM utworzeniu PI (kind=ebook, email=NULL - placeholder
    # nie istnieje, order powstaje przy potwierdzeniu z realnym emailem).
    await attribution.store(
        db,
        kind="ebook",
        stripe_object_id=intent.id,
        email=None,
        attribution=payload.attribution,
        client_ip=client_ip(request),
        client_ua=request.headers.get("user-agent"),
    )
    await db.commit()

    return dump(
        EbookPaymentIntentOut(client_secret=intent.client_secret or "", payment_intent_id=intent.id)
    )


@router.post("/confirm")
async def confirm_purchase(
    payload: EbookConfirmIn,
    db: AsyncSession = Depends(get_session),
):
    """Przyspieszacz UX (front retry'uje 8x2s). Gwarancja fulfillmentu =
    webhook payment_intent.succeeded ([billing-webhook]) przez ten sam serwis."""
    client = get_client(StripeAccount.CURRENT)
    pi = await client.v1.payment_intents.retrieve_async(payload.payment_intent_id)
    if pi.status != "succeeded":
        # 409 + pole `status` 1:1 z oryginalem - front na tym opiera retry.
        return JSONResponse(
            {"error": f"Payment status: {pi.status}", "status": pi.status}, status_code=409
        )

    try:
        result = await fulfill_ebook_order(
            pi,
            email=payload.email,
            wants_invoice=payload.want_invoice,
            nip=payload.nip,
            invoice_name=payload.invoice_name,
            session=db,
        )
    except EbookFulfillmentError as err:
        raise HTTPException(err.status, str(err)) from err
    await db.commit()

    return dump(
        EbookConfirmOut(
            success=True, email=result.email, download_url=result.download_url, token=result.token
        )
    )


@router.get("/download")
async def download_ebook(
    token: str | None = None,
    db: AsyncSession = Depends(get_session),
) -> FileResponse:
    if not token:
        raise HTTPException(400, "Brak tokenu")

    try:
        consumed = await consume_download_token(token, session=db)
    except DownloadTokenError as err:
        raise HTTPException(err.status, err.message) from err
    await db.commit()

    # Streaming z dysku (wolumen) zamiast signed URL Supabase Storage;
    # content-disposition z nazwa pliku 1:1.
    return FileResponse(
        consumed.file_path,
        media_type="application/pdf",
        filename=consumed.filename,
    )
