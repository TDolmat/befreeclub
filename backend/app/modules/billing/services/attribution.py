"""Zapis atrybucji checkoutu (billing.checkout_attributions).

Twarde wymaganie analityki (PLAN_LANDING, decyzje 2026-06-10): KAZDE
utworzenie obiektu platnosci w Stripe (SetupIntent, Checkout Session,
PaymentIntent) dostaje wiersz atrybucji, nawet gdy front nie przyslal
zadnych UTM-ow (wtedy wiersz z samym kind/stripe_object_id/ip/ua -
odroznia "organic" od "brak zapisu").

Sygnatura zamrozona w port-kontrakt-2.md sekcja 3.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.email import normalize_email
from app.modules.billing.models import CheckoutAttribution
from app.modules.billing.schemas import AttributionIn


async def store(
    session: AsyncSession,
    *,
    kind: str,  # "subscription" | "klarna" | "ebook"
    stripe_object_id: str,  # seti_... | cs_... | pi_...
    email: str | None = None,
    attribution: AttributionIn | None = None,
    client_ip: str | None = None,
    client_ua: str | None = None,
) -> CheckoutAttribution:
    """Insert wiersza atrybucji. Caller robi commit (ten sam transaction co
    reszta operacji checkoutu)."""
    a = attribution or AttributionIn()
    row = CheckoutAttribution(
        kind=kind,
        email=normalize_email(email) if email else None,
        stripe_object_id=stripe_object_id,
        utm_source=a.utm_source,
        utm_medium=a.utm_medium,
        utm_campaign=a.utm_campaign,
        utm_term=a.utm_term,
        utm_content=a.utm_content,
        fbclid=a.fbclid,
        fbp=a.fbp,
        fbc=a.fbc,
        referrer=a.referrer,
        landing_page=a.landing_page,
        client_ip=client_ip,
        client_ua=client_ua,
    )
    session.add(row)
    await session.flush()
    return row
