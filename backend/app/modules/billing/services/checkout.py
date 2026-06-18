"""Port checkoutu subskrypcji i Klarny (spec: billing-checkout.md sekcje 4 i 6).

Edge functions -> serwisy:
- create-checkout          -> create_setup_intent
- confirm-subscription     -> confirm_subscription
- create-klarna-checkout   -> create_klarna_session
- confirm-klarna-checkout  -> confirm_klarna

Semantyka biznesowa 1:1 (SetupIntent usage=off_session pod mandat SCA -
NIE zastepowac Checkout Sessionem; subscriptions.create error_if_incomplete
+ off_session; cichy fallback promo) z naprawami z PLAN_LANDING i
port-kontrakt-2.md:
- plany/kwoty/price ID z billing.plans zamiast PRICE_MAP/PLAN_CONFIG,
- normalize_email na kazdym wejsciu; lookup customera z fallbackiem
  customers.search (case-insensitive - filtr email w customers.list jest
  case-sensitive, a legacy customerzy bywaja z wielka litera; review 2.1),
- Idempotency-Key przy KAZDYM create w Stripe,
- atrybucja UTM/Meta (kontrakt 5.1): wiersz billing.checkout_attributions
  dla subskrypcji zapisuje DOPIERO confirm (prefetch frontu nie smieci
  tabela) + kopia utm_*/fbclid/fbp/fbc w metadata Stripe (SetupIntent,
  subskrypcja, sesja Klarny + jej payment_intent),
- blokada rownoleglej DRUGIEJ subskrypcji (dlug #12 ze speca), rozszerzona
  w review 2.1: OBA konta, wszyscy customerzy emaila, statusy
  active/trialing/past_due/unpaid (pauza adminowa/przedluzenie zyja jako
  trialing, a past_due ma Smart Retries - bez tego podwojne obciazenia),
- Klarna: wspolny grant_one_time_access (services/klarna_grant.py) zamiast
  trzech kopii, z odmowa gdy charge zrefundowany; expires_at kotwiczone
  w session.created (nie "teraz") - reconcile co godzine nie pelza terminem,
- plany sprzedawalne TYLKO na koncie current (guard; promo lookup,
  confirm_klarna i fulfillment sa current-only),
- bledy biznesowe jako HTTP 4xx (kontrakt 1.1), komunikaty 1:1 z oryginalu.

KONTRAKT ANALITYKI (port-kontrakt-2.md 5.3): Meta CAPI Purchase NIE strzela
z tych endpointow - robi to WYLACZNIE webhook ([billing-webhook]:
invoice.payment_succeeded z billing_reason=subscription_create dla suba,
checkout.session.completed/async_payment_succeeded dla Klarny), zeby
konwersja liczyla sie takze bez powrotu przegladarki. Endpointy potwierdzen
zwracaja tylko id pod eventID pixela (latestInvoiceId / paymentIntentId).
"""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import stripe
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.email import normalize_email
from app.core.logging import create_logger
from app.core.stripe_client import (
    StripeAccount,
    get_client,
    new_idempotency_key,
    request_options,
    search_customers_by_email,
)
from app.modules.billing.models import CheckoutAttribution
from app.modules.billing.schemas import AttributionIn
from app.modules.billing.services import attribution as attribution_service
from app.modules.billing.services import plans as plans_service
from app.modules.billing.services import promo as promo_service
from app.modules.billing.services import subscriptions as subscriptions_service
from app.modules.billing.services.klarna_grant import (
    PaymentRefundedError,
    grant_one_time_access,
)
from app.modules.members.services import provisioning

log = create_logger("billing.checkout")

# Interwal planu -> dlugosc dostepu jednorazowego w miesiacach.
INTERVAL_MONTHS = {"quarter": 3, "half_year": 6, "year": 12}
# Klarna TYLKO dla 6/12 miesiecy (swiadomy prog oplacalnosci - bez quarterly).
KLARNA_INTERVALS = {"half_year", "year"}

DEFAULT_ORIGIN = "https://befreeclub.pl"

# Statusy traktowane jako "zywa suba" przy blokadzie drugiego zakupu
# (szersze niz "active": pauza/przedluzenie admina = trialing, nieoplacone
# odnowienie = past_due/unpaid, ktore Smart Retries moga jeszcze sciagnac).
BLOCKING_SUB_STATUSES = {"active", "trialing", "past_due", "unpaid"}

# Nowy komunikat (rozszerzenie portu, dlug #12 speca): blokada drugiej
# rownoleglej subskrypcji zamiast podwojnych obciazen.
SECOND_PLAN_MESSAGE = (
    "Masz już aktywną subskrypcję Be Free Club. "
    "Żeby zmienić plan, napisz na krystian@befreeclub.pl."
)
# Nowy komunikat (naprawa prowizorki #1): zrefundowana platnosc Klarna.
REFUNDED_MESSAGE = "Płatność została zwrócona. Napisz na krystian@befreeclub.pl, jeśli to pomyłka."

_ATTRIBUTION_META_FIELDS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "fbp",
    "fbc",
)


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    """Bezpieczny odczyt pola StripeObject (v15 nie jest dict, brak .get())."""
    if obj is None:
        return default
    try:
        return obj[key]
    except (KeyError, TypeError):
        return default


def _obj_id(value: Any) -> str | None:
    """Pole expandowalne Stripe: string id albo obiekt z polem id."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return _obj_get(value, "id")


def _meta_dict(meta: Any) -> dict:
    if meta is None:
        return {}
    if hasattr(meta, "to_dict"):
        return dict(meta.to_dict())
    return dict(meta)


def _attribution_metadata(source: AttributionIn | CheckoutAttribution | None) -> dict[str, str]:
    """utm_*/fbclid/fbp/fbc do metadata Stripe (kontrakt 5.1) - tylko niepuste."""
    if source is None:
        return {}
    out: dict[str, str] = {}
    for field in _ATTRIBUTION_META_FIELDS:
        value = getattr(source, field, None)
        if value:
            out[field] = value
    return out


async def _load_attribution(
    session: AsyncSession, stripe_object_id: str
) -> CheckoutAttribution | None:
    stmt = (
        select(CheckoutAttribution)
        .where(CheckoutAttribution.stripe_object_id == stripe_object_id)
        .order_by(CheckoutAttribution.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


def _require_current_account(plan) -> None:
    """Plan sprzedawalny MUSI byc na koncie current (guard, review 2.1).

    Promo lookup, confirm_klarna, webhook fulfillment i klucz publishable
    frontu sa current-only - plan zaseedowany na legacy dalby pobrana
    platnosc bez nadania dostepu. Sprzedaz na legacy jest wykluczona
    decyzja biznesowa, wiec to blad konfiguracji (500), nie usera.
    """
    if plan.stripe_account != StripeAccount.CURRENT.value:
        log.error(f"Plan {plan.slug} is configured on '{plan.stripe_account}' Stripe account")
        raise HTTPException(500, f"Plan {plan.slug} is not sellable (legacy Stripe account)")


async def _require_subscription_plan(session: AsyncSession, plan_id: str | None):
    """Plan z billing.plans: musi istniec, byc aktywny i byc subskrypcyjny."""
    plan = await plans_service.get_by_slug(plan_id or "", session=session)
    if plan is None or not plan.active or plan.interval == "one_time":
        # Komunikat 1:1 z create-checkout/confirm-subscription; 400 wg 1.1.
        raise HTTPException(400, f"Invalid plan: {plan_id}")
    _require_current_account(plan)
    return plan


# ── create-checkout -> POST /checkout/setup-intent ───────────────────────────


async def create_setup_intent(
    session: AsyncSession,
    *,
    plan_id: str | None,
    attribution: AttributionIn | None,
    client_ip: str | None,
    client_ua: str | None,
) -> dict:
    """SetupIntent usage=off_session - SEDNO konstrukcji SCA (1:1 z oryginalem).

    Podczas pierwszego 3DS bank dostaje mandat "karta bedzie obciazana bez
    udzialu klienta" - bez tego polskie banki (mBank, ING, Santander, PKO)
    odrzucaly odnowienia z authentication_required. Zero platnosci na tym
    etapie - obciazenie robi dopiero confirm_subscription.
    """
    plan = await _require_subscription_plan(session, plan_id)
    client = get_client(StripeAccount(plan.stripe_account))

    metadata = {
        "price_id": plan.stripe_price_id,
        "plan_id": plan.slug,
        **_attribution_metadata(attribution),
    }
    setup_intent = await client.v1.setup_intents.create_async(
        params={
            "payment_method_types": ["card"],
            "usage": "off_session",
            "metadata": metadata,
        },
        options=request_options(idempotency_key=new_idempotency_key("checkout-setup")),
    )
    log.info(f"SetupIntent: {setup_intent.id}")

    # ZERO zapisu atrybucji tutaj (kontrakt 5.1): wiersz checkout_attributions
    # tworzy DOPIERO confirm - prefetch frontu (kazdy wizytator po 5 s)
    # nie smieci tabela. utm-y i tak leca w metadata SetupIntentu wyzej.
    return {"clientSecret": setup_intent.client_secret, "setupIntentId": setup_intent.id}


# ── confirm-subscription -> POST /checkout/confirm ───────────────────────────


async def confirm_subscription(
    session: AsyncSession,
    *,
    setup_intent_id: str | None,
    plan_id: str | None,
    email: str | None,
    want_invoice: bool,
    nip: str | None,
    promo_code: str | None,
    attribution: AttributionIn | None,
    client_ip: str | None,
    client_ua: str | None,
) -> dict:
    if not setup_intent_id or not plan_id or not email:
        raise HTTPException(400, "Missing required fields: setupIntentId, planId, email")

    email = normalize_email(email)
    plan = await _require_subscription_plan(session, plan_id)
    price_id = plan.stripe_price_id
    client = get_client(StripeAccount(plan.stripe_account))

    # Promo: server-side, cichy fallback (zakup idzie bez rabatu) - 1:1.
    promotion_code_id: str | None = None
    if promo_code and isinstance(promo_code, str):
        promo = await promo_service.lookup_active_promotion_code(promo_code)
        if promo is not None:
            promotion_code_id = promo.id

    setup_intent = await client.v1.setup_intents.retrieve_async(setup_intent_id)
    if setup_intent.status != "succeeded":
        raise HTTPException(409, f"SetupIntent not succeeded: {setup_intent.status}")

    payment_method_id = _obj_id(_obj_get(setup_intent, "payment_method"))
    if not payment_method_id:
        raise HTTPException(409, "No payment method on SetupIntent")

    # Rekoncyliacja Customer <-> PaymentMethod (edge case'y Apple/Google Pay):
    # PM moze przyjsc juz przypiety do customera - wtedy uzywamy TEGO
    # customera, nawet gdy email sie rozni od podanego (1:1 z oryginalem).
    pm = await client.v1.payment_methods.retrieve_async(payment_method_id)
    pm_customer_id = _obj_id(_obj_get(pm, "customer"))

    # Lookup z fallbackiem search (case-insensitive): legacy customer
    # z "Jan@X.pl" w Stripe MUSI zostac znaleziony, inaczej powstaje duplikat
    # i druga pelnoplatna suba (review 2.1).
    existing_customer_list = await search_customers_by_email(client, email, limit=1)

    customer: Any
    created_new_customer = False

    async def _create_customer() -> Any:
        params: dict[str, Any] = {"email": email}
        if want_invoice and nip:
            params["metadata"] = {"wants_invoice": "true", "nip": nip}
        return await client.v1.customers.create_async(
            params=params,
            options=request_options(idempotency_key=f"cus-create-{setup_intent_id}"),
        )

    if pm_customer_id:
        log.info(f"PM already attached to customer {pm_customer_id}")
        pm_customer = await client.v1.customers.retrieve_async(pm_customer_id)
        if not _obj_get(pm_customer, "deleted"):
            customer = pm_customer
        else:
            # Customer PM-a usuniety: detach i reuse/utworzenie po emailu.
            log.info("PM customer was deleted, detaching PM")
            await client.v1.payment_methods.detach_async(payment_method_id)
            if existing_customer_list:
                customer = existing_customer_list[0]
            else:
                customer = await _create_customer()
                created_new_customer = True
            await client.v1.payment_methods.attach_async(
                payment_method_id, params={"customer": customer.id}
            )
    elif existing_customer_list:
        customer = existing_customer_list[0]
        log.info(f"Reusing existing customer: {customer.id}")
        await client.v1.payment_methods.attach_async(
            payment_method_id, params={"customer": customer.id}
        )
    else:
        customer = await _create_customer()
        created_new_customer = True
        log.info(f"Customer created: {customer.id}")
        await client.v1.payment_methods.attach_async(
            payment_method_id, params={"customer": customer.id}
        )

    def _sub_has_price(sub: Any) -> bool:
        return any(
            _obj_get(_obj_get(item, "price"), "id") == price_id
            for item in (_obj_get(_obj_get(sub, "items"), "data") or [])
        )

    # Idempotencja per-customer (1:1): aktywna subskrypcja z TYM SAMYM
    # price = sukces bez akcji. Per-customer zostaje OPROCZ checku po emailu
    # nizej, bo Apple/Google Pay potrafi rozwiazac customera z INNYM emailem.
    existing_subs = await client.v1.subscriptions.list_async(
        params={"customer": customer.id, "status": "active", "limit": 10}
    )
    already_subscribed = any(_sub_has_price(sub) for sub in existing_subs.data)
    if already_subscribed:
        log.info(f"Already subscribed, skipping: {email}")
        return {
            "subscriptionId": existing_subs.data[0].id,
            "status": "active",
            "alreadyExisted": True,
        }

    # ROZSZERZENIE portu (dlug #12 + review 2.1): blokada drugiej rownoleglej
    # suby po CALYM emailu - oba konta, wszyscy customerzy, statusy zywe
    # (trialing = pauza/przedluzenie admina, past_due/unpaid = Smart Retries
    # moga jeszcze sciagnac stara sube). Ten sam plan + active gdziekolwiek
    # = idempotentny sukces; cokolwiek innego zywego = 409.
    live_subs = await subscriptions_service.find_subscriptions_by_email(
        email, statuses=BLOCKING_SUB_STATUSES
    )
    same_plan_active = next(
        (
            item.subscription
            for item in live_subs
            if _obj_get(item.subscription, "status") == "active"
            and _sub_has_price(item.subscription)
        ),
        None,
    )
    if same_plan_active is not None:
        log.info(f"Already subscribed (email-wide), skipping: {email}")
        return {
            "subscriptionId": same_plan_active.id,
            "status": "active",
            "alreadyExisted": True,
        }
    if existing_subs.data or live_subs:
        log.warn(f"Live subscription already exists, blocking second plan: {email}")
        raise HTTPException(409, SECOND_PLAN_MESSAGE)

    if want_invoice and nip:
        await client.v1.customers.update_async(
            customer.id,
            params={
                "metadata": {
                    **_meta_dict(_obj_get(customer, "metadata")),
                    "wants_invoice": "true",
                    "nip": nip,
                }
            },
        )

    await client.v1.customers.update_async(
        customer.id,
        params={"invoice_settings": {"default_payment_method": payment_method_id}},
    )

    # NIP jako Tax ID tylko dla swiezo utworzonego customera, blad ignorowany (1:1).
    if created_new_customer and want_invoice and nip:
        try:
            await client.v1.customers.tax_ids.create_async(
                customer.id,
                params={"type": "pl_nip", "value": nip},
                options=request_options(idempotency_key=f"taxid-{setup_intent_id}"),
            )
            log.info(f"Tax ID (NIP) added: {nip}")
        except Exception as err:
            log.error(f"Failed to add tax ID: {err}")

    # Atrybucja (kontrakt 5.1): zapis robi confirm. Wiersz moze juz istniec
    # po retrym confirma - wtedy backfill emaila + merge niepustych pol
    # z requestu (nie gubimy utm/fbp/fbc przyslanych pozniej).
    attribution_row = await _load_attribution(session, setup_intent_id)
    if attribution_row is None:
        attribution_row = await attribution_service.store(
            session,
            kind="subscription",
            stripe_object_id=setup_intent_id,
            email=email,
            attribution=attribution,
            client_ip=client_ip,
            client_ua=client_ua,
        )
    else:
        if not attribution_row.email:
            attribution_row.email = email
        if attribution is not None:
            for field in (*_ATTRIBUTION_META_FIELDS, "referrer", "landing_page"):
                value = getattr(attribution, field, None)
                if value and not getattr(attribution_row, field, None):
                    setattr(attribution_row, field, value)
    await session.commit()

    sub_params: dict[str, Any] = {
        "customer": customer.id,
        "items": [{"price": price_id}],
        "default_payment_method": payment_method_id,
        # error_if_incomplete: odmowa pierwszego obciazenia = blad (user widzi
        # komunikat, srodki nie schodza). off_session: uzyj mandatu SCA
        # z SetupIntentu - klucz do bezproblemowych odnowien (1:1).
        "payment_behavior": "error_if_incomplete",
        "off_session": True,
        "payment_settings": {
            "payment_method_types": ["card"],
            "save_default_payment_method": "on_subscription",
        },
        "metadata": {"plan_id": plan.slug, **_attribution_metadata(attribution_row)},
    }
    if promotion_code_id:
        sub_params["discounts"] = [{"promotion_code": promotion_code_id}]

    try:
        subscription = await client.v1.subscriptions.create_async(
            params=sub_params,
            options=request_options(idempotency_key=f"sub-create-{setup_intent_id}"),
        )
    except stripe.CardError as err:
        log.warn(f"Subscription create declined for {email}: {err.user_message or err}")
        raise HTTPException(402, err.user_message or str(err)) from err
    except stripe.StripeError as err:
        log.error(f"Subscription create failed for {email}: {err}")
        raise HTTPException(502, err.user_message or str(err)) from err

    log.info(f"Subscription created: {subscription.id} status: {subscription.status}")

    if subscription.status != "active":
        # Front traktuje nie-active jako nieudana platnosc (1:1: 200 + status).
        log.error(f"Subscription not active ({subscription.status}) for {email}")
        return {
            "subscriptionId": subscription.id,
            "status": subscription.status,
            "circleInvited": False,
        }

    # Invite do Circle + utrwalenie czlonka - members.provision zamiast
    # copy-paste inviteToCircle+upsert. Nieudany invite NIE wycofuje
    # subskrypcji (provision nie rzuca, status invite_failed do recznej akcji).
    result = await provisioning.provision(email, None, source="subscription")
    if not result.circle_invited:
        log.error(
            f"Subscription created but Circle invite FAILED for {email}. "
            "Manual intervention may be needed."
        )

    # KONTRAKT: CAPI Purchase strzela WYLACZNIE webhook (invoice.payment_succeeded
    # z billing_reason=subscription_create) - patrz docstring modulu.
    # latestInvoiceId wraca dla eventID pixela Purchase na froncie.
    return {
        "subscriptionId": subscription.id,
        "status": subscription.status,
        "circleInvited": result.circle_invited,
        "latestInvoiceId": _obj_id(_obj_get(subscription, "latest_invoice")),
    }


# ── create-klarna-checkout -> POST /checkout/klarna ──────────────────────────


async def create_klarna_session(
    session: AsyncSession,
    *,
    plan_id: str | None,
    email: str | None,
    promo_code: str | None,
    attribution: AttributionIn | None,
    origin: str | None,
    client_ip: str | None,
    client_ua: str | None,
) -> dict:
    """Stripe Checkout Session mode=payment (Klarna nie wspiera off_session
    recurring). Platnosc jednorazowa za caly okres, dostep czasowy expires_at.
    Tor przyjmuje tez card i blik jednorazowo - swiadomie, jak oryginal."""
    plan = await plans_service.get_by_slug(plan_id or "", session=session)
    if plan is None or not plan.active or plan.interval not in KLARNA_INTERVALS:
        raise HTTPException(400, f"Klarna nie jest dostępna dla planu: {plan_id}")
    _require_current_account(plan)
    duration_months = INTERVAL_MONTHS[plan.interval]
    client = get_client(StripeAccount(plan.stripe_account))

    promotion_code_id: str | None = None
    if promo_code and isinstance(promo_code, str):
        promo = await promo_service.lookup_active_promotion_code(promo_code)
        if promo is not None:
            promotion_code_id = promo.id

    base_url = origin or settings.FRONTEND_URL or DEFAULT_ORIGIN
    # source=klarna_checkout to dyskryminator dla webhooka/reconcile (1:1);
    # kopia metadata w payment_intent_data + utm (kontrakt 5.1).
    metadata = {
        "source": "klarna_checkout",
        "plan_id": plan.slug,
        "duration_months": str(duration_months),
        **_attribution_metadata(attribution),
    }
    params: dict[str, Any] = {
        "mode": "payment",
        "payment_method_types": ["klarna", "card", "blik"],
        # price_data ad-hoc: recurring price nie przejdzie w mode=payment (1:1).
        "line_items": [
            {
                "price_data": {
                    "currency": "pln",
                    "product_data": {
                        "name": f"Be Free Club - {duration_months} miesięcy",
                        "description": (
                            f"Dostęp do społeczności Be Free Club przez {duration_months} "
                            "miesięcy. Płatność jednorazowa."
                        ),
                    },
                    "unit_amount": plan.amount_pln,
                },
                "quantity": 1,
            }
        ],
        "success_url": (
            f"{base_url}/sukces?source=klarna&plan={quote(plan.slug, safe='')}"
            "&session_id={CHECKOUT_SESSION_ID}"
        ),
        "cancel_url": f"{base_url}/?checkout_failed=true&planId={quote(plan.slug, safe='')}",
        "metadata": metadata,
        "payment_intent_data": {"metadata": metadata},
        "locale": "pl",
        "billing_address_collection": "auto",
    }
    if email:
        params["customer_email"] = normalize_email(email)
    else:
        params["customer_creation"] = "always"
    if promotion_code_id:
        params["discounts"] = [{"promotion_code": promotion_code_id}]

    checkout_session = await client.v1.checkout.sessions.create_async(
        params=params,
        options=request_options(idempotency_key=new_idempotency_key("klarna")),
    )
    log.info(f"Klarna session created: {checkout_session.id} for plan: {plan.slug}")

    # email=NULL - autorytatywny email zbiera Stripe Checkout (kontrakt 5.1).
    await attribution_service.store(
        session,
        kind="klarna",
        stripe_object_id=checkout_session.id,
        email=None,
        attribution=attribution,
        client_ip=client_ip,
        client_ua=client_ua,
    )
    await session.commit()

    return {"url": checkout_session.url, "sessionId": checkout_session.id}


# ── confirm-klarna-checkout -> POST /checkout/klarna/confirm ─────────────────


async def confirm_klarna(session: AsyncSession, *, session_id: str | None) -> dict:
    """Przyspieszacz UX po powrocie z /sukces?source=klarna. Zrodlem prawdy
    jest webhook - obie sciezki ida przez WSPOLNY grant_one_time_access
    (idempotentny, expires tylko w gore, mail powitalny raz)."""
    if not session_id or not isinstance(session_id, str):
        raise HTTPException(400, "Missing sessionId")

    client = get_client(StripeAccount.CURRENT)
    checkout_session = await client.v1.checkout.sessions.retrieve_async(session_id)

    metadata = _obj_get(checkout_session, "metadata")
    if _obj_get(metadata, "source") != "klarna_checkout":
        raise HTTPException(400, "Checkout session is not a Klarna checkout")

    payment_status = _obj_get(checkout_session, "payment_status")
    if payment_status != "paid":
        # Klarna potrafi potwierdzac asynchronicznie (minuty-dni) - front czeka.
        raise HTTPException(409, f"Payment is not paid yet: {payment_status}")

    raw_email = _obj_get(checkout_session, "customer_email") or _obj_get(
        _obj_get(checkout_session, "customer_details"), "email"
    )
    email = normalize_email(raw_email or "")
    if not email:
        raise HTTPException(400, "Missing customer email on checkout session")

    plan_slug = _obj_get(metadata, "plan_id") or "semiannual"
    try:
        duration_months = int(_obj_get(metadata, "duration_months") or 0)
    except (TypeError, ValueError):
        duration_months = 0
    if not duration_months:
        # Fallback 1:1 z PLAN_DURATIONS, tylko ze z billing.plans.
        plan = await plans_service.get_by_slug(plan_slug, session=session)
        duration_months = INTERVAL_MONTHS.get(plan.interval, 0) if plan else 0
    if not duration_months:
        raise HTTPException(400, "Missing duration for Klarna checkout")

    payment_intent_id = _obj_id(_obj_get(checkout_session, "payment_intent"))
    created_ts = _obj_get(checkout_session, "created")
    purchased_at = (
        datetime.fromtimestamp(int(created_ts), UTC) if created_ts else None
    )

    try:
        result = await grant_one_time_access(
            email=email,
            duration_months=duration_months,
            payment_intent_id=payment_intent_id,
            purchased_at=purchased_at,
        )
    except PaymentRefundedError as err:
        raise HTTPException(409, REFUNDED_MESSAGE) from err

    log.info(f"confirm-klarna {email}: paid={payment_status}, invited={result.circle_invited}")

    # paymentIntentId wraca dla eventID pixela Purchase (kontrakt 5.2);
    # CAPI Purchase strzela webhook, nie ten endpoint.
    if result.already_active:
        return {
            "success": True,
            "alreadyActive": True,
            "email": email,
            "paymentIntentId": payment_intent_id,
        }
    if not result.circle_invited:
        # 1:1: platnosc przeszla, invite padl -> 502, front pokazuje toast
        # "wymaga recznego sprawdzenia"; nastepny grant/retry sprobuje znowu.
        raise HTTPException(502, "Circle invite failed")
    return {
        "success": True,
        "email": email,
        "circleMemberId": result.circle_member_id,
        "paymentIntentId": payment_intent_id,
    }
