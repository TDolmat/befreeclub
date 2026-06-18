"""Strzaly Meta CAPI Purchase z webhooka Stripe (kontrakt 5.2/5.3).

Purchase strzela WYLACZNIE webhook handler - konwersja liczy sie nawet bez
powrotu przegladarki. event_id deterministyczny (deduplikacja z pikselem):
- subskrypcja -> id pierwszej faktury suba (`in_...`),
- Klarna -> payment_intent id sesji (`pi_...`),
- ebook -> payment_intent id (`pi_...`).

user_data: email z eventu (znormalizowany, hashuje klient CAPI), fbp/fbc/
client_ip/client_ua z billing.checkout_attributions (lookup po
stripe_object_id albo email) z fallbackiem na metadata Stripe. Gdy brak fbc,
a jest fbclid: budujemy `fb.1.<created_at_ms_atrybucji>.<fbclid>`.
custom_data: value w PLN floatem (amount/100), currency, content_name =
slug planu / "ebook". event_source_url: landing_page z atrybucji
(fallback https://befreeclub.pl).

Wysylka best-effort: meta_capi.send_event nigdy nie rzuca - analityka nie
moze wywracac obslugi platnosci. Idempotencja: webhook_events gwarantuje
jednorazowa obsluge eventu; przy retrym po bledzie Meta deduplikuje po
event_id.
"""

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import meta_capi
from app.core.email import normalize_email
from app.core.logging import create_logger
from app.core.meta_capi import CapiCustomData, CapiUserData
from app.modules.billing.models import CheckoutAttribution

log = create_logger("billing.capi")

DEFAULT_EVENT_SOURCE_URL = "https://befreeclub.pl"


def _obj_id(value: Any) -> str | None:
    """Pole expandowalne Stripe: string id albo obiekt z polem id."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return value["id"]
    except (KeyError, TypeError):
        return None


async def _attribution_for(
    session: AsyncSession,
    *,
    stripe_object_id: str | None = None,
    email: str | None = None,
    kind: str | None = None,
) -> CheckoutAttribution | None:
    """Atrybucja: najpierw po stripe_object_id, fallback po emailu (najnowsza)."""
    if stripe_object_id:
        stmt = (
            select(CheckoutAttribution)
            .where(CheckoutAttribution.stripe_object_id == stripe_object_id)
            .order_by(CheckoutAttribution.id.desc())
            .limit(1)
        )
        row = (await session.execute(stmt)).scalars().first()
        if row is not None:
            return row
    if email:
        stmt = (
            select(CheckoutAttribution)
            .where(CheckoutAttribution.email == normalize_email(email))
            .order_by(CheckoutAttribution.id.desc())
            .limit(1)
        )
        if kind:
            stmt = (
                select(CheckoutAttribution)
                .where(
                    CheckoutAttribution.email == normalize_email(email),
                    CheckoutAttribution.kind == kind,
                )
                .order_by(CheckoutAttribution.id.desc())
                .limit(1)
            )
        return (await session.execute(stmt)).scalars().first()
    return None


def _build_fbc(
    attribution: CheckoutAttribution | None, metadata: dict[str, Any], event_time: int
) -> str | None:
    """fbc wprost albo zbudowany z fbclid: fb.1.<created_at_ms>.<fbclid>."""
    if attribution is not None and attribution.fbc:
        return attribution.fbc
    if metadata.get("fbc"):
        return str(metadata["fbc"])
    if attribution is not None and attribution.fbclid:
        ms = int(attribution.created_at.timestamp() * 1000)
        return f"fb.1.{ms}.{attribution.fbclid}"
    if metadata.get("fbclid"):
        # Brak wiersza atrybucji (np. checkout sprzed migracji) - timestamp eventu.
        return f"fb.1.{event_time * 1000}.{metadata['fbclid']}"
    return None


def _user_data(
    email: str | None,
    attribution: CheckoutAttribution | None,
    metadata: dict[str, Any],
    event_time: int,
) -> CapiUserData:
    fbp = (attribution.fbp if attribution is not None else None) or metadata.get("fbp")
    return CapiUserData(
        email=email,
        fbc=_build_fbc(attribution, metadata, event_time),
        fbp=str(fbp) if fbp else None,
        client_ip=attribution.client_ip if attribution is not None else None,
        client_ua=attribution.client_ua if attribution is not None else None,
    )


def _source_url(attribution: CheckoutAttribution | None) -> str:
    if attribution is not None and attribution.landing_page:
        return attribution.landing_page
    return DEFAULT_EVENT_SOURCE_URL


async def purchase_from_subscription_invoice(
    session: AsyncSession, invoice: dict[str, Any], *, event_time: int | None = None
) -> bool:
    """Purchase pierwszej faktury subskrypcji. event_id = id faktury (`in_...`).

    Caller filtruje billing_reason == "subscription_create" (odnowienia NIE
    sa Purchase). Atrybucja po emailu (kind=subscription) - faktura nie nosi
    setup intentu; fbp/fbc fallback ze snapshotu metadata subskrypcji na
    fakturze (oba ksztalty: parent.subscription_details i stare pole).
    """
    event_id = invoice.get("id")
    if not event_id:
        log.warn("CAPI Purchase (sub): invoice without id, skipping")
        return False
    ts = event_time or int(time.time())

    sub_details = (invoice.get("parent") or {}).get("subscription_details") or invoice.get(
        "subscription_details"
    ) or {}
    metadata = sub_details.get("metadata") or {}

    raw_email = invoice.get("customer_email")
    email = normalize_email(raw_email) if raw_email else None
    attribution = await _attribution_for(session, email=email, kind="subscription")

    amount = invoice.get("amount_paid")
    if amount is None:
        amount = invoice.get("amount_due") or 0

    return await meta_capi.send_event(
        event_name="Purchase",
        event_id=str(event_id),
        event_time=ts,
        user_data=_user_data(email, attribution, metadata, ts),
        custom_data=CapiCustomData(
            value=amount / 100,
            currency=str(invoice.get("currency") or "pln"),
            content_name=metadata.get("plan_id"),
        ),
        event_source_url=_source_url(attribution),
    )


async def purchase_from_klarna_session(
    session: AsyncSession, checkout_session: dict[str, Any], *, event_time: int | None = None
) -> bool:
    """Purchase oplaconej sesji Klarna. event_id = payment_intent sesji (`pi_...`)."""
    pi_id = _obj_id(checkout_session.get("payment_intent"))
    if not pi_id:
        log.warn(
            f"CAPI Purchase (klarna): no payment_intent on session "
            f"{checkout_session.get('id')}, skipping"
        )
        return False
    ts = event_time or int(time.time())

    metadata = checkout_session.get("metadata") or {}
    raw_email = checkout_session.get("customer_email") or (
        checkout_session.get("customer_details") or {}
    ).get("email")
    email = normalize_email(raw_email) if raw_email else None
    attribution = await _attribution_for(
        session, stripe_object_id=checkout_session.get("id"), email=email
    )

    return await meta_capi.send_event(
        event_name="Purchase",
        event_id=pi_id,
        event_time=ts,
        user_data=_user_data(email, attribution, metadata, ts),
        custom_data=CapiCustomData(
            value=(checkout_session.get("amount_total") or 0) / 100,
            currency=str(checkout_session.get("currency") or "pln"),
            content_name=metadata.get("plan_id"),
        ),
        event_source_url=_source_url(attribution),
    )


async def purchase_from_ebook_payment_intent(
    session: AsyncSession,
    payment_intent: dict[str, Any],
    *,
    email: str | None = None,
    event_time: int | None = None,
) -> bool:
    """Purchase ebooka. event_id = payment_intent id (`pi_...`)."""
    event_id = payment_intent.get("id")
    if not event_id:
        log.warn("CAPI Purchase (ebook): payment intent without id, skipping")
        return False
    ts = event_time or int(time.time())

    metadata = payment_intent.get("metadata") or {}
    raw_email = email or payment_intent.get("receipt_email")
    normalized = normalize_email(raw_email) if raw_email else None
    attribution = await _attribution_for(
        session, stripe_object_id=str(event_id), email=normalized
    )

    return await meta_capi.send_event(
        event_name="Purchase",
        event_id=str(event_id),
        event_time=ts,
        user_data=_user_data(normalized, attribution, metadata, ts),
        custom_data=CapiCustomData(
            value=(payment_intent.get("amount") or 0) / 100,
            currency=str(payment_intent.get("currency") or "pln"),
            content_name="ebook",
        ),
        event_source_url=_source_url(attribution),
    )
