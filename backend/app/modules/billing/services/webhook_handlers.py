"""Handlery eventow webhooka Stripe (port stripe-webhook/index.ts, OBA konta).

Semantyka 1:1 z oryginalem + naprawy z PLAN_LANDING ("Naprawy wpisane w port")
i port-kontrakt-2.md wiersz 8/9:
- invoice.payment_failed: mail "płatność nie powiodła się" 1:1 (hosted_invoice_url
  CTA, link /aktualizuj-karte, numer proby w temacie, data nastepnej proby pl-PL);
  subskrypcja czytana z OBU pol - starego `invoice.subscription` i nowego
  `invoice.parent.subscription_details.subscription` (naprawa #1: webhook glchy
  na nieudane platnosci przy nowym API). Dziala tez dla konta legacy (naprawa #4).
  Powod nieudanej platnosci zostaje w billing.webhook_events.payload (panel
  czyta historie stamtad, nie ze Stripe).
- checkout.session.completed / async_payment_succeeded (Klarna): wspolny
  grant_one_time_access (refund-check, expires tylko w gore, mail powitalny raz)
  + Meta CAPI Purchase (event_id = payment_intent sesji).
- payment_intent.succeeded z metadata.product == "ebook": fulfillment
  webhook-first przez wspolny fulfill_ebook_order (order+token+mail bez udzialu
  przegladarki) + CAPI Purchase (event_id = pi). Gdy PI nie ma receipt_email,
  fallback na billing_details.email z latest_charge (hardening review 2.1 -
  webhook-first nie moze stac w 100% na dyscyplinie frontu).
- charge.refunded: FILTR PO PRODUKCIE (naprawa #5 - najgrubsza mina oryginalu):
  refund ebooka -> TYLKO invalidate_ebook_tokens (zero ruszania subskrypcji
  i Circle) + tombstone "refunded" gdy refund wyprzedzil fulfillment (kolejnosc
  eventow Stripe nie jest gwarantowana - bez tombstone pozniejszy
  payment_intent.succeeded wydalby ebooka po zwrocie); refund subskrypcji/
  Klarny -> natychmiastowy cancel wszystkich subow emaila na OBU kontach (1:1)
  + members.schedule_removal (fizyczne usuniecie z Circle robi cleanup worker).
  Email customera doczytywany na koncie, z ktorego przyszedl event.
  Blad odczytu PI przy rozstrzyganiu produktu PROPAGUJE (fail-closed, review
  2.1): refund bez rozstrzygnietego produktu nie moze wpasc w sciezke
  czlonkowska i skasowac subskrypcji po przejsciowym 5xx Stripe.
- invoice.paid / invoice.payment_succeeded: CAPI Purchase tylko dla
  billing_reason == subscription_create (odnowienia NIE sa Purchase); kazda
  oplacona faktura emaila zdejmuje tez members.status "paused" -> "active"
  (naturalne wznowienie po pauzie adminowej, kontrakt #16); poza tym event
  tylko zapisany (historia dla panelu).
- customer.subscription.updated|deleted i kazdy inny typ: tylko zapis eventu
  (robi to routes/webhooks.py przed dispatch'em).

Wyjatek z handlera = wpis `error` w webhook_events (processed_at NULL),
response 200 - robi to routes/webhooks.py; reczne ponowienie: POST
/api/billing/admin/webhook-events/{id}/reprocess. Swiadoma zmiana vs
oryginal: blad wysylki maila payment_failed NIE jest polykany, zeby panel
widzial niedostarczone maile.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.email import DEFAULT_FROM, normalize_email, send_email
from app.core.logging import create_logger
from app.core.stripe_client import (
    StripeAccount,
    configured_accounts,
    get_client,
    search_customers_by_email,
)
from app.modules.billing.services import capi_events, klarna_grant
from app.modules.billing.services.ebook import (
    fulfill_ebook_order,
    invalidate_ebook_tokens,
    record_refund_tombstone,
)
from app.modules.billing.services.klarna_grant import PaymentRefundedError, format_pl_date
from app.modules.members.services import provisioning

log = create_logger("billing.webhook")

# 1:1 z oryginalem (hardcoded w mailu, nie FRONTEND_URL - tresc bajt w bajt).
UPDATE_CARD_URL = "https://befreeclub.pl/aktualizuj-karte"

# Statusy terminalne - nie do anulowania przy refundzie (1:1).
_TERMINAL_SUB_STATUSES = ("canceled", "incomplete_expired")


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


def _invoice_subscription_id(invoice: dict[str, Any]) -> str | None:
    """Subskrypcja faktury z OBU pol (naprawa #1): stare `invoice.subscription`
    i nowe `invoice.parent.subscription_details.subscription` (API basil)."""
    sub = _obj_id(invoice.get("subscription"))
    if sub:
        return sub
    details = (invoice.get("parent") or {}).get("subscription_details") or {}
    return _obj_id(details.get("subscription"))


async def process_event(
    session: AsyncSession, account: StripeAccount, event: dict[str, Any]
) -> None:
    """Dispatch eventu webhooka. Event jest juz zapisany w webhook_events
    (idempotencja) - tu wylacznie skutki biznesowe. Wyjatek propaguje do
    route'a (wpis error, response 200)."""
    event_type = str(event.get("type") or "")
    obj: dict[str, Any] = (event.get("data") or {}).get("object") or {}
    created = event.get("created")
    event_time = int(created) if created else None

    if event_type == "invoice.payment_failed":
        await _handle_payment_failed(obj)
    elif event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        await _handle_checkout_session(session, event_type, obj, event_time)
    elif event_type == "payment_intent.succeeded":
        await _handle_payment_intent_succeeded(session, account, obj, event_time)
    elif event_type == "charge.refunded":
        await _handle_refund(session, account, obj)
    elif event_type in ("invoice.paid", "invoice.payment_succeeded"):
        await _handle_invoice_paid(session, obj, event_time)
    else:
        # invoice.paid/customer.subscription.updated|deleted i reszta: tylko
        # zapis eventu (zrobiony przed dispatch'em) - panel czyta historie.
        log.info(f"Stored event without handler: {event_type}")


# ── invoice.payment_failed ────────────────────────────────────────────────────


async def _handle_payment_failed(invoice: dict[str, Any]) -> None:
    if not _invoice_subscription_id(invoice):
        log.info("Skipping non-subscription invoice")
        return

    email = invoice.get("customer_email")
    if not email:
        log.info("No customer email, skipping")
        return

    hosted_invoice_url = invoice.get("hosted_invoice_url") or ""
    if not hosted_invoice_url:
        log.error(f"No hosted_invoice_url on failed invoice {invoice.get('id')}")
        return

    amount_due = invoice.get("amount_due") or 0
    currency = str(invoice.get("currency") or "")
    amount = f"{amount_due / 100:.2f} {currency.upper()}"
    attempt_count = invoice.get("attempt_count") or 1
    next_payment_attempt = invoice.get("next_payment_attempt")
    next_attempt = (
        format_pl_date(datetime.fromtimestamp(next_payment_attempt, UTC))
        if next_payment_attempt
        else None
    )

    await send_payment_failed_email(
        email=normalize_email(str(email)),
        amount=amount,
        hosted_invoice_url=hosted_invoice_url,
        attempt_count=attempt_count,
        next_attempt=next_attempt,
    )


async def send_payment_failed_email(
    *,
    email: str,
    amount: str,
    hosted_invoice_url: str,
    attempt_count: int,
    next_attempt: str | None,
) -> None:
    """Mail o nieudanej platnosci - tresc i temat 1:1 ze stripe-webhook/index.ts.

    Rzuca EmailConfigError/EmailSendError (swiadoma zmiana vs oryginal, ktory
    polykal blad Resend): route zapisze blad do webhook_events dla panelu.
    """
    is_first_attempt = attempt_count == 1
    subject = (
        "⚠️ Płatność za Be Free Club nie powiodła się - autoryzuj jednym kliknięciem"
        if is_first_attempt
        else f"⚠️ Ponowna próba {attempt_count} - płatność za Be Free Club nie powiodła się"
    )

    next_attempt_text = (
        f'<p style="color:#666;font-size:14px;">Następna próba pobrania środków: '
        f"<strong>{next_attempt}</strong></p>"
        if next_attempt
        else ""
    )

    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px; background: #ffffff; color: #1a1a1a;">
      <div style="text-align: center; margin-bottom: 24px;">
        <h1 style="color: #1a1a1a; font-size: 22px; margin: 0;">Płatność wymaga Twojego potwierdzenia</h1>
      </div>

      <p>Cześć,</p>

      <p>Twoja karta nie została obciążona kwotą <strong>{amount}</strong> za odnowienie subskrypcji Be Free Club. Najczęściej dzieje się to dlatego, że Twój bank wymaga dodatkowego potwierdzenia (3D Secure).</p>

      <p style="font-size:16px;"><strong>Najszybsze rozwiązanie - jedno kliknięcie:</strong></p>

      <div style="text-align: center; margin: 24px 0;">
        <a href="{hosted_invoice_url}" style="display:inline-block;background:#e2c96a;color:#1a1a1a;padding:14px 28px;border-radius:10px;text-decoration:none;font-weight:bold;font-size:16px;">
          Zapłać i autoryzuj kartę →
        </a>
      </div>

      <p style="color:#666;font-size:13px;text-align:center;">
        Klikasz link, potwierdzasz płatność w aplikacji bankowej i gotowe.<br>
        Twoja subskrypcja zostanie aktywna od razu, bez przerwy w dostępie do społeczności.
      </p>

      <hr style="border:none;border-top:1px solid #e5e5e5;margin:24px 0;">

      <p style="font-size:14px;"><strong>Chcesz zmienić kartę na inną?</strong></p>
      <p style="font-size:14px;">Zaktualizuj swoją metodę płatności tutaj: <a href="{UPDATE_CARD_URL}" style="color:#1a1a1a;">{UPDATE_CARD_URL}</a></p>

      {next_attempt_text}

      <p style="color:#666;font-size:13px;margin-top:24px;">
        Jeśli płatność nie zostanie autoryzowana, automatycznie ponowimy próbę. Po wszystkich nieudanych próbach Twój dostęp do społeczności zostanie wstrzymany.
      </p>

      <p style="margin-top:32px;">Pozdrawiam,<br><strong>Krystian</strong><br>Be Free Club</p>

      <p style="color:#999;font-size:12px;margin-top:24px;text-align:center;">
        Masz pytania? Odpisz na ten email lub napisz na <a href="mailto:krystian@befreeclub.pl" style="color:#999;">krystian@befreeclub.pl</a>
      </p>
    </div>
  """

    await send_email(
        to=email,
        subject=subject,
        html=html,
        from_email=DEFAULT_FROM,
        reply_to="krystian@befreeclub.pl",
    )
    log.info(f"Payment failed email sent to {email}")


# ── checkout.session.completed / async_payment_succeeded (Klarna) ─────────────


async def _handle_checkout_session(
    session: AsyncSession,
    event_type: str,
    checkout_session: dict[str, Any],
    event_time: int | None,
) -> None:
    metadata = checkout_session.get("metadata") or {}
    if metadata.get("source") != "klarna_checkout":
        log.info(f"{event_type} but not klarna_checkout, skipping")
        return

    payment_status = checkout_session.get("payment_status")
    if payment_status != "paid":
        # Klarna pending: czekamy na async_payment_succeeded (1:1).
        log.info(f"Klarna session not paid yet (status={payment_status}), waiting for async event")
        return

    raw_email = checkout_session.get("customer_email") or (
        checkout_session.get("customer_details") or {}
    ).get("email")
    if not raw_email:
        log.error(f"No email on Klarna session {checkout_session.get('id')}")
        return

    try:
        duration_months = int(metadata.get("duration_months") or 0)
    except (TypeError, ValueError):
        duration_months = 0
    if not duration_months:
        # 1:1: webhook bez fallbacku na plan (w przeciwienstwie do confirm).
        log.error(f"Invalid duration_months on Klarna session {checkout_session.get('id')}")
        return

    created_ts = checkout_session.get("created")
    purchased_at = datetime.fromtimestamp(int(created_ts), UTC) if created_ts else None
    try:
        result = await klarna_grant.grant_one_time_access(
            email=str(raw_email),
            duration_months=duration_months,
            payment_intent_id=_obj_id(checkout_session.get("payment_intent")),
            purchased_at=purchased_at,
        )
    except PaymentRefundedError:
        # Naprawa prowizorki #1: zrefundowana sesja (payment_status dalej
        # "paid") nie nadaje dostepu ani nie liczy konwersji.
        log.warn(f"Klarna session {checkout_session.get('id')} refunded, access NOT granted")
        return

    log.info(
        f"Klarna webhook grant: {normalize_email(str(raw_email))} "
        f"invited={result.circle_invited} already_active={result.already_active}"
    )
    await capi_events.purchase_from_klarna_session(
        session, checkout_session, event_time=event_time
    )


# ── payment_intent.succeeded (ebook, webhook-first fulfillment) ───────────────


async def _ebook_fallback_email(
    account: StripeAccount, payment_intent: dict[str, Any]
) -> str | None:
    """Email z billing_details latest_charge gdy PI nie ma receipt_email
    (hardening review 2.1: PaymentElement zawsze zbiera billing email,
    receipt_email zalezy od dyscypliny frontu). Blad odczytu propaguje -
    event laduje jako error do recznego reprocess."""
    charge = payment_intent.get("latest_charge")
    if isinstance(charge, dict):
        email = (charge.get("billing_details") or {}).get("email")
        if email:
            return str(email)
        charge = charge.get("id")
    if not charge:
        return None
    client = get_client(account)
    fetched = await client.v1.charges.retrieve_async(str(charge))
    return _obj_get(_obj_get(fetched, "billing_details"), "email")


async def _handle_payment_intent_succeeded(
    session: AsyncSession,
    account: StripeAccount,
    payment_intent: dict[str, Any],
    event_time: int | None,
) -> None:
    metadata = payment_intent.get("metadata") or {}
    if metadata.get("product") != "ebook":
        log.info("payment_intent.succeeded but not ebook, skipping")
        return

    email: str | None = None
    if not payment_intent.get("receipt_email"):
        email = await _ebook_fallback_email(account, payment_intent)

    # Wspolny serwis z POST /ebook/confirm - idempotentny (order+token+mail RAZ).
    result = await fulfill_ebook_order(payment_intent, email=email, session=session)
    log.info(
        f"Ebook fulfilled via webhook: {payment_intent.get('id')} "
        f"({result.email}, mail_sent={result.email_sent})"
    )
    await capi_events.purchase_from_ebook_payment_intent(
        session, payment_intent, email=result.email, event_time=event_time
    )


# ── charge.refunded ───────────────────────────────────────────────────────────


async def _handle_refund(
    session: AsyncSession, account: StripeAccount, charge: dict[str, Any]
) -> None:
    charge_id = charge.get("id")
    amount = charge.get("amount") or 0
    amount_refunded = charge.get("amount_refunded") or 0

    # Tylko PELNE refundy, czesciowe ignorowane (1:1).
    is_full_refund = charge.get("refunded") is True and amount_refunded >= amount
    if not is_full_refund:
        log.info(
            f"Charge {charge_id} is partial refund ({amount_refunded}/{amount}), skipping"
        )
        return

    payment_intent_id = _obj_id(charge.get("payment_intent"))
    product = await _resolve_charge_product(account, charge, payment_intent_id)

    if product == "ebook":
        # NAPRAWA #5: refund ebooka uniewaznia TYLKO tokeny pobrania.
        # Zero ruszania subskrypcji i Circle.
        email = (charge.get("billing_details") or {}).get("email") or charge.get("receipt_email")
        if payment_intent_id:
            revoked = await invalidate_ebook_tokens(
                payment_intent_id=payment_intent_id, session=session
            )
            # Refund moze wyprzedzic payment_intent.succeeded (Stripe nie
            # gwarantuje kolejnosci) - tombstone "refunded" blokuje pozniejszy
            # fulfillment zwroconej platnosci (review 2.1).
            if email:
                await record_refund_tombstone(
                    payment_intent_id=payment_intent_id,
                    email=str(email),
                    amount=amount,
                    currency=str(charge.get("currency") or "pln"),
                    session=session,
                )
        elif email:
            revoked = await invalidate_ebook_tokens(email=str(email), session=session)
        else:
            log.error(f"Ebook refund {charge_id} without payment_intent and email, skipping")
            return
        log.info(f"Ebook refund {charge_id}: revoked {revoked} download token(s)")
        return

    # Refund subskrypcji / platnosci jednorazowej (Klarna): jak oryginal.
    email = (charge.get("billing_details") or {}).get("email") or charge.get("receipt_email")
    if not email and charge.get("customer"):
        # Doczytanie customera na koncie, z ktorego przyszedl event.
        email = await _customer_email(account, _obj_id(charge.get("customer")))
    if not email:
        log.error(f"No email found for charge {charge_id}, skipping")
        return

    normalized_email = normalize_email(str(email))
    log.info(
        f"Full refund for {normalized_email} "
        f"(charge {charge_id}, {amount_refunded}/{amount})"
    )

    # 1. Natychmiastowy cancel wszystkich subskrypcji na OBU kontach (1:1).
    total_cancelled = 0
    for acct in configured_accounts():
        total_cancelled += await cancel_subscriptions_immediately(acct, normalized_email)

    # 2. Deprovisioning przez members (status pending_removal + event;
    # fizyczne usuniecie z Circle robi cleanup worker). Chronieni: no-op.
    await provisioning.schedule_removal(normalized_email, reason="refund")

    log.info(
        f"Refund done for {normalized_email}: {total_cancelled} sub(s) cancelled, "
        "removal scheduled"
    )


async def _resolve_charge_product(
    account: StripeAccount, charge: dict[str, Any], payment_intent_id: str | None
) -> str | None:
    """Produkt charge'a z metadata (filtr naprawy #5). Stripe kopiuje metadata
    PaymentIntenta na charge - a gdyby nie skopiowal, doczytujemy PI na koncie
    eventu (pas i szelki: od tego filtra zalezy czy refund ebooka nie skasuje
    czlonkostwa w klubie).

    Blad odczytu PI PROPAGUJE (fail-closed, review 2.1): None oznacza
    "rozstrzygniete: nie-ebook" i wpada w sciezke czlonkowska (cancel subow
    + removal), wiec przejsciowy 5xx Stripe nie moze udawac rozstrzygniecia.
    Event laduje jako error do recznego reprocess."""
    metadata = charge.get("metadata") or {}
    product = metadata.get("product")
    if product:
        return str(product)
    if not payment_intent_id:
        return None
    client = get_client(account)
    pi = await client.v1.payment_intents.retrieve_async(payment_intent_id)
    pi_product = _obj_get(_obj_get(pi, "metadata"), "product")
    return str(pi_product) if pi_product else None


async def _customer_email(account: StripeAccount, customer_id: str | None) -> str | None:
    if not customer_id:
        return None
    try:
        client = get_client(account)
        customer = await client.v1.customers.retrieve_async(customer_id)
    except Exception as err:
        log.error(f"Failed to retrieve customer ({account.value}): {err}")
        return None
    if _obj_get(customer, "deleted"):
        return None
    return _obj_get(customer, "email")


async def cancel_subscriptions_immediately(account: StripeAccount, email: str) -> int:
    """Anuluje NATYCHMIAST wszystkie nie-terminalne subskrypcje emaila na
    koncie (1:1 z cancelSubscriptionsImmediately: max 5 customerow, status=all
    limit 20, bledy pojedynczych canceli polykane, blad konta -> 0).
    Lookup z fallbackiem customers.search (case-insensitive, review 2.1)."""
    try:
        client = get_client(account)
        customers = await search_customers_by_email(client, email, limit=5)
        cancelled = 0
        for customer in customers:
            subs = await client.v1.subscriptions.list_async(
                params={"customer": customer.id, "status": "all", "limit": 20}
            )
            for sub in subs.data:
                if sub.status in _TERMINAL_SUB_STATUSES:
                    continue
                try:
                    await client.v1.subscriptions.cancel_async(sub.id)
                    cancelled += 1
                    log.info(
                        f"Immediately cancelled sub {sub.id} ({account.value}) for {email}"
                    )
                except Exception as err:
                    log.error(f"Failed to cancel {sub.id}: {err}")
        return cancelled
    except Exception as err:
        log.error(f"Error cancelling on {account.value}: {err}")
        return 0


# ── invoice.paid / invoice.payment_succeeded (CAPI Purchase suba) ─────────────


async def _handle_invoice_paid(
    session: AsyncSession, invoice: dict[str, Any], event_time: int | None
) -> None:
    # Oplacona faktura = naturalne wznowienie po pauzie adminowej: flip
    # members.status "paused" -> "active" (no-op dla pozostalych statusow).
    # Bez tego status "paused" zostawalby w DB na zawsze (kontrakt #16).
    customer_email = invoice.get("customer_email")
    if customer_email:
        await provisioning.set_pause_state(
            normalize_email(str(customer_email)), False, by="stripe-webhook"
        )

    if invoice.get("billing_reason") != "subscription_create":
        # Odnowienia NIE sa Purchase (kontrakt 5.3) - event tylko zapisany.
        log.info(f"Invoice {invoice.get('id')} is not first subscription invoice, stored only")
        return
    await capi_events.purchase_from_subscription_invoice(
        session, invoice, event_time=event_time
    )
