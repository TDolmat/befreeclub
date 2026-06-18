"""DTO modulu billing (pydantic, camelCase przez CamelModel).

Fundament definiuje TYLKO kontrakt wspolny: AttributionIn (ksztalt pol
atrybucji w KAZDYM requestcie checkoutu - twarde wymaganie analityki)
i PlanOut (publiczny odczyt planow). Reszte DTO dopisuja agenci
[billing-checkout], [billing-ebook], [billing-lifecycle], [billing-webhook],
[admin-api] - patrz docs/spec-landing/port-kontrakt-2.md.
"""

from typing import Any
from uuid import UUID

from app.core.schemas import CamelModel, IsoDateTime
from app.modules.members.schemas import MemberOut


class AttributionIn(CamelModel):
    """Atrybucja UTM/Meta doklejana przez front do checkoutow.

    JSON: {"attribution": {"utmSource": ..., "fbclid": ..., "landingPage": ...}}.
    Wszystkie pola opcjonalne - brak pola = None. Front zbiera je na wejsciu
    na strone (sessionStorage) i dokleja do create-checkout / klarna / ebook.
    """

    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_term: str | None = None
    utm_content: str | None = None
    fbclid: str | None = None
    fbp: str | None = None
    fbc: str | None = None
    referrer: str | None = None
    landing_page: str | None = None


class PlanOut(CamelModel):
    """Publiczna prezentacja planu (GET /api/billing/plans)."""

    slug: str
    name: str
    amount_pln: int  # grosze
    interval: str
    sort: int


class PlanAdminOut(PlanOut):
    """Plan w panelu admina (z price ID i stanem)."""

    id: int
    stripe_price_id: str
    stripe_account: str
    active: bool
    created_at: IsoDateTime
    updated_at: IsoDateTime


# ── [billing-ebook] ───────────────────────────────────────────────────────────


class EbookPaymentIntentIn(CamelModel):
    """Body POST /api/billing/ebook/payment-intent. Oryginal slal {} -
    wszystko opcjonalne. Dane fakturowe w BODY (naprawa dlugu #5: koniec
    z query stringiem), laduja w metadata PI dla webhook-first fulfillmentu."""

    attribution: AttributionIn | None = None
    want_invoice: bool = False
    nip: str | None = None
    invoice_name: str | None = None


class EbookPaymentIntentOut(CamelModel):
    """Response 1:1 z create-ebook-payment-intent."""

    client_secret: str
    payment_intent_id: str


class EbookConfirmIn(CamelModel):
    """Body POST /api/billing/ebook/confirm. Tylko sciezka paymentIntentId
    (sessionId byl martwym flow B). Dane fakturowe w body, nie query stringu."""

    payment_intent_id: str
    email: str | None = None
    want_invoice: bool = False
    nip: str | None = None
    invoice_name: str | None = None


class EbookConfirmOut(CamelModel):
    """Response 1:1 z confirm-ebook-purchase: {success, email, downloadUrl, token}."""

    success: bool
    email: str
    download_url: str
    token: str


# ── [billing-checkout] DTO requestow checkoutu ───────────────────────────────
# Pola Optional + reczna walidacja w serwisach: komunikaty bledow musza byc
# 1:1 z oryginalem (np. "Missing required fields: setupIntentId, planId,
# email"), a globalny handler walidacji pydantic daje generyczne
# {"error": "Invalid request"}. Response'y to dicty 1:1 z oryginalem
# (klucze camelCase pisane recznie w services/checkout.py i promo.py).


class CheckoutSetupIntentIn(CamelModel):
    """POST /checkout/setup-intent (port create-checkout)."""

    plan_id: str | None = None
    attribution: AttributionIn | None = None


class CheckoutConfirmIn(CamelModel):
    """POST /checkout/confirm (port confirm-subscription)."""

    setup_intent_id: str | None = None
    plan_id: str | None = None
    email: str | None = None
    want_invoice: bool = False
    nip: str | None = None
    promo_code: str | None = None
    attribution: AttributionIn | None = None


class KlarnaCheckoutIn(CamelModel):
    """POST /checkout/klarna (port create-klarna-checkout)."""

    plan_id: str | None = None
    email: str | None = None
    promo_code: str | None = None
    attribution: AttributionIn | None = None


class KlarnaConfirmIn(CamelModel):
    """POST /checkout/klarna/confirm (port confirm-klarna-checkout)."""

    session_id: str | None = None


class PromoValidateIn(CamelModel):
    """POST /promo/validate (port validate-promo)."""

    code: str | None = None


# ── [billing-lifecycle] ───────────────────────────────────────────────────────
# Pola Optional + walidacja w serwisach (komunikaty 1:1 z oryginalem, np.
# "Email jest wymagany"); populate_by_name przyjmuje tez snake_case jak
# w bodach oryginalnych funkcji admin-* ({freeze_days, remove_from_circle}).


class CancellationRequestIn(CamelModel):
    """POST /cancellation/request (port request-cancellation)."""

    email: str | None = None
    reason: str | None = None


class CancellationConfirmIn(CamelModel):
    """POST /cancellation/confirm (port confirm-cancellation)."""

    token: str | None = None


class PaymentMethodRequestLinkIn(CamelModel):
    """POST /payment-method/request-link (naprawa #2: magic link na maila)."""

    email: str | None = None


class PaymentMethodSetupIntentIn(CamelModel):
    """POST /payment-method/setup-intent (port ?action=create-intent + token)."""

    token: str | None = None


class PaymentMethodConfirmIn(CamelModel):
    """POST /payment-method/confirm (port ?action=confirm; po 3DS front
    bierze setupIntentId z parametrow return_url Stripe)."""

    setup_intent_id: str | None = None


class AdminPauseIn(CamelModel):
    """POST /admin/subscriptions/pause (port admin-pause-subscription)."""

    email: str | None = None
    freeze_days: int | None = None
    remove_from_circle: bool = False


class AdminExtendIn(CamelModel):
    """POST /admin/subscriptions/extend (port admin-extend-subscription 1:1)."""

    subscription_id: str | None = None
    email: str | None = None
    account: str = "current"
    resumes_at: int | None = None
    trial_end: int | None = None
    add_months: int | None = None
    clear_pause: bool = False


class AdminCancelIn(CamelModel):
    """POST /admin/subscriptions/cancel (NOWE - akcja panelu wg PLAN_LANDING:
    anuluj na koniec okresu albo natychmiast)."""

    email: str | None = None
    at_period_end: bool = True


class CancellationReasonOut(CamelModel):
    """Wiersz billing.cancellation_reasons (GET /admin/cancellations)."""

    id: UUID
    email: str
    reason: str
    action: str
    freeze_days: int | None
    created_at: IsoDateTime


# ── [admin-api] panel Subskrypcje (GET /admin/subscribers, /problems) ─────────
# NOWE endpointy bez odpowiednika w edge functions - JSON w camelCase przez
# CamelModel (konwencja nowych API), nie 1:1 z zadnym oryginalem.


class AdminSubscriptionOut(CamelModel):
    """Subskrypcja Stripe w widoku panelu (snapshot/zywe dane jednego konta)."""

    id: str
    account: str  # current | legacy
    status: str
    plan_slug: str | None  # match price ID -> billing.plans; None = nieznany plan
    price_id: str | None
    amount_pln: int | None  # GROSZE (jak billing.plans.amount_pln)
    interval: str | None
    current_period_end: IsoDateTime | None
    cancel_at_period_end: bool
    pause_resumes_at: IsoDateTime | None
    card_brand: str | None
    card_last4: str | None
    card_exp_month: int | None
    card_exp_year: int | None
    card_expires_before_renewal: bool
    created_at: IsoDateTime | None


class AdminLastEventOut(CamelModel):
    """Ostatni webhook event emaila (lista subskrybentow)."""

    type: str
    account: str
    created_at: IsoDateTime
    processed: bool
    error: str | None


class AdminSubscriberRowOut(CamelModel):
    """Wiersz listy: members + zywe suby Stripe + ostatni webhook event."""

    email: str
    member: MemberOut | None
    subscriptions: list[AdminSubscriptionOut]
    last_webhook_event: AdminLastEventOut | None


class AdminSubscriberListOut(CamelModel):
    subscribers: list[AdminSubscriberRowOut]
    total: int
    page: int
    page_size: int


class AdminTimelineEntryOut(CamelModel):
    """Wpis timeline'u karty osoby. source: webhook | member | admin |
    cancellation; kind: typ eventu / akcja; detail: podsumowanie (klucze
    camelCase pisane recznie w services/admin_subscribers.py)."""

    at: IsoDateTime
    source: str
    kind: str
    detail: dict[str, Any]


class AdminAttributionOut(CamelModel):
    """Atrybucja ostatniego checkoutu (skad przyszedl - UTM)."""

    kind: str
    stripe_object_id: str
    utm_source: str | None
    utm_medium: str | None
    utm_campaign: str | None
    utm_term: str | None
    utm_content: str | None
    fbclid: str | None
    referrer: str | None
    landing_page: str | None
    created_at: IsoDateTime


class AdminSubscriberDetailOut(CamelModel):
    """Karta osoby: stan czlonka, suby na zywo (oba konta), timeline,
    atrybucja ostatniego checkoutu."""

    email: str
    member: MemberOut | None
    subscriptions: list[AdminSubscriptionOut]
    timeline: list[AdminTimelineEntryOut]
    attribution: AdminAttributionOut | None


class AdminFailedRenewalOut(CamelModel):
    """Nieudane odnowienie do obslugi (payment_failed bez pozniejszego paid)."""

    email: str | None
    account: str
    subscription_id: str | None
    invoice_id: str | None
    amount_due: int | None  # grosze
    currency: str | None
    attempt_count: int | None
    next_payment_attempt: IsoDateTime | None
    hosted_invoice_url: str | None
    failed_at: IsoDateTime
    event_id: str


class AdminExpiringCardOut(AdminSubscriptionOut):
    """Karta wygasajaca przed nastepnym odnowieniem (logika legacy-audit,
    oba konta)."""

    email: str | None


class AdminProblemsOut(CamelModel):
    failed_renewals: list[AdminFailedRenewalOut]
    expiring_cards: list[AdminExpiringCardOut]
