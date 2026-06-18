"""Port update-payment-method Z NAPRAWA #2 z PLAN_LANDING.

Oryginal (spec: billing-checkout.md sekcja 7): zero autoryzacji ("podaj email
i hulaj"), tylko konto current (legacy odciete od zmiany karty), zepsuty
powrot z 3DS. Port:

1. request_link: email -> magic link HMAC na maila (TEN SAM wzorzec
   i sekret co anulowanie, payload z purpose=update_payment_method, exp
   +60 min). Publiczny endpoint zwraca ZAWSZE {"ok": true} (anty-enumeracja,
   swiadoma zmiana vs 404 oryginalu); panel admina ("wyslij link zmiany
   karty") dostaje prawdziwy wynik.
2. create_setup_intent: z WAZNYM tokenem; szuka klienta na OBU kontach
   (naprawa: legacy odzyskuje zmiane karty), SetupIntent usage=off_session
   na koncie, gdzie zyje subskrypcja.
3. confirm: attach PM jako default na customerze + wszystkich subach
   active/past_due/unpaid/trialing + retry otwartych faktur - 1:1 ze spec.
   Dziala tez po powrocie z 3DS: front trzyma token w return_url i wola
   /confirm z setupIntentId z URL (naprawiony powrot - oryginalna strona
   gubila parametry). setupIntentId dziala jak bearer (1:1 z oryginalem),
   dlatego confirm nie wymaga ponownie tokenu.
"""

import time
from urllib.parse import quote

import stripe
from fastapi import HTTPException

from app.core import email as email_core
from app.core.config import settings
from app.core.email import DEFAULT_FROM, EmailConfigError, EmailSendError, normalize_email
from app.core.logging import create_logger
from app.core.stripe_client import (
    StripeAccount,
    configured_accounts,
    get_client,
    new_idempotency_key,
    request_options,
    search_customers_by_email,
)
from app.modules.billing.services import magic_link
from app.modules.billing.services.subscriptions import obj_get

log = create_logger("billing.pm")

TOKEN_PURPOSE = "update_payment_method"
TOKEN_TTL_MS = 60 * 60 * 1000
DEFAULT_FRONTEND_URL = "https://befreeclub.pl"

# Statusy subskrypcji "wartych" zmiany karty - 1:1 z oryginalem.
RELEVANT_STATUSES = {"active", "past_due", "unpaid", "trialing"}

# Komunikaty 1:1 z update-payment-method/index.ts.
INVALID_EMAIL_MESSAGE = "Wpisz prawidłowy adres email."
NO_ACCOUNT_MESSAGE = "Nie znaleźliśmy konta z takim adresem email."
NO_SUBSCRIPTION_MESSAGE = "Nie znaleźliśmy aktywnej subskrypcji powiązanej z tym adresem email."
MISSING_CARD_DATA_MESSAGE = "Brak danych karty lub klienta."
# Nowy komunikat (flow magic linka nie istnial w oryginale) - wzor z anulowania.
INVALID_TOKEN_MESSAGE = (
    "Link wygasł lub jest nieprawidłowy. Wróć na stronę zmiany karty i wyślij nowy."
)

EMAIL_SUBJECT = "Zmiana karty płatniczej Be Free Club"
EMAIL_REPLY_TO = "kontakt@befreeclub.pl"


def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


# Nowy mail (oryginal nie wysylal zadnego) - szablon ciemnej karty BFC
# 1:1 strukturalnie z mailem anulowania, copy pod zmiane karty.
_EMAIL_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<title>Zmień kartę płatniczą</title>
<style>
  :root { color-scheme: light only; supported-color-schemes: light only; }
  @media (prefers-color-scheme: dark) {
    .bfc-card { background:#2c2d31 !important; }
    .bfc-bg { background:#1a1b1f !important; }
    .bfc-btn { background:#ECE183 !important; }
    .bfc-btn-link { color:#1a1b1f !important; }
    .bfc-h1 { color:#ffffff !important; }
    .bfc-body { color:#cfd1d4 !important; }
    .bfc-muted { color:#a8aab0 !important; }
    .bfc-faint { color:#666 !important; }
    .bfc-eyebrow { color:#999 !important; }
    .bfc-brand { color:#ECE183 !important; }
  }
</style>
</head>
<body class="bfc-bg" style="margin:0;padding:0;background:#1a1b1f;font-family:'Space Grotesk','Inter',Arial,sans-serif;-webkit-text-size-adjust:100%;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="bfc-bg" style="background:#1a1b1f;">
    <tr><td align="center" style="padding:40px 16px;">
      <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" class="bfc-card" style="max-width:560px;width:100%;background:#2c2d31;border-radius:16px;border:1px solid #3a3b3f;">
        <tr><td style="padding:40px 32px;">
          <p class="bfc-eyebrow" style="font-size:12px;color:#999;letter-spacing:1px;text-transform:uppercase;margin:0 0 20px;font-family:Arial,sans-serif;">Zmiana karty płatniczej <span class="bfc-brand" style="color:#ECE183;">Be Free Club</span></p>
          <h1 class="bfc-h1" style="font-size:24px;font-weight:700;line-height:1.3;color:#ffffff;margin:0 0 20px;font-family:'Space Grotesk',Arial,sans-serif;">
            Zaktualizuj kartę płatniczą
          </h1>
          <p class="bfc-body" style="font-size:16px;line-height:1.6;color:#cfd1d4;margin:0 0 14px;">
            Otrzymaliśmy prośbę o zmianę karty płatniczej do Twojej subskrypcji. Kliknij poniższy przycisk, żeby bezpiecznie podać dane nowej karty.
          </p>
          <p class="bfc-body" style="font-size:16px;line-height:1.6;color:#cfd1d4;margin:0 0 28px;">
            Nowa karta zostanie podpięta do Twojej subskrypcji. Jeśli masz zaległą płatność, spróbujemy ją od razu opłacić nową kartą.
          </p>
          <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 28px;"><tr><td align="center" class="bfc-btn" bgcolor="#ECE183" style="border-radius:12px;background:#ECE183;">
            <a href="__SAFE_URL__" class="bfc-btn-link" style="display:inline-block;padding:14px 32px;font-size:16px;font-weight:700;color:#1a1b1f;text-decoration:none;font-family:'Space Grotesk',Arial,sans-serif;border-radius:12px;">
              Zmieniam kartę
            </a>
          </td></tr></table>
          <p class="bfc-muted" style="font-size:13px;line-height:1.6;color:#888;margin:0;">
            Link wygasa za 60 minut. Jeśli to nie Ty zainicjowałeś zmianę, zignoruj ten mail. Nic się nie zmieni.
          </p>
          <p class="bfc-faint" style="font-size:12px;line-height:1.6;color:#a8aab0;margin:24px 0 0;border-top:1px solid #3a3b3f;padding-top:16px;font-family:Arial,sans-serif;">
            Nie działa przycisk? Skopiuj ten link do przeglądarki:<br>
            <a href="__SAFE_URL__" class="bfc-brand" style="color:#ECE183 !important;text-decoration:underline;word-break:break-all;">__SAFE_URL__</a>
          </p>
        </td></tr>
      </table>
      <p class="bfc-faint" style="font-size:12px;color:#666;margin:24px 0 0;font-family:Arial,sans-serif;">
        <a href="https://befreeclub.pl/" class="bfc-brand" style="color:#ECE183;text-decoration:none;">Be Free Club</a> &middot; <a href="https://www.instagram.com/krystianbefree/" class="bfc-brand" style="color:#ECE183;text-decoration:none;">Krystian Rudnik</a>
      </p>
    </td></tr>
  </table>
</body></html>"""


def build_email_html(update_url: str) -> str:
    return _EMAIL_HTML_TEMPLATE.replace("__SAFE_URL__", _escape_html(update_url))


def _obj_id(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return obj_get(value, "id")


def _require_doi_secret() -> str:
    secret = settings.CANCELLATION_DOI_SECRET
    if not secret:
        raise HTTPException(500, "CANCELLATION_DOI_SECRET not set")
    return secret


async def _find_customer_with_relevant_sub(
    email: str,
) -> tuple[StripeAccount | None, str | None, bool]:
    """Pierwszy customer z subem w RELEVANT_STATUSES, OBA konta
    (current -> legacy). Zwraca (account, customer_id, czy_jakikolwiek
    customer istnieje) - flaga rozroznia komunikaty 404 oryginalu.
    Lookup z fallbackiem customers.search (case-insensitive, review 2.1)."""
    any_customer = False
    for account in configured_accounts():
        client = get_client(account)
        customers = await search_customers_by_email(client, email, limit=100)
        if customers:
            any_customer = True
        for customer in customers:
            subs = await client.v1.subscriptions.list_async(
                params={"customer": customer.id, "status": "all", "limit": 100}
            )
            if any(obj_get(s, "status") in RELEVANT_STATUSES for s in subs.data):
                return account, customer.id, True
    return None, None, any_customer


# ── POST /payment-method/request-link (publiczny) + admin send-link ──────────


async def request_link(*, email: str | None, raise_on_send_error: bool = False) -> dict:
    """Wysyla magic link zmiany karty, jesli email ma subskrypcje.

    Zwraca wynik WEWNETRZNY {"found", "sent", "account"} - publiczny route
    i tak odpowiada zawsze {"ok": true} (anty-enumeracja); szczegoly konsumuje
    panel admina. raise_on_send_error=True (admin): blad Resend -> 502.
    """
    if not email or not isinstance(email, str) or "@" not in email:
        raise HTTPException(400, INVALID_EMAIL_MESSAGE)
    email = normalize_email(email)
    secret = _require_doi_secret()

    account, _, _ = await _find_customer_with_relevant_sub(email)
    if account is None:
        log.info(f"PM link requested for unknown/sub-less email: {email}")
        return {"found": False, "sent": False, "account": None}

    token = magic_link.sign_token(
        {"email": email, "exp": int(time.time() * 1000) + TOKEN_TTL_MS, "purpose": TOKEN_PURPOSE},
        secret,
    )
    frontend = settings.FRONTEND_URL or DEFAULT_FRONTEND_URL
    update_url = f"{frontend}/aktualizuj-karte?token={quote(token, safe='')}"

    try:
        await email_core.send_email(
            to=email,
            subject=EMAIL_SUBJECT,
            html=build_email_html(update_url),
            from_email=settings.CANCELLATION_FROM_EMAIL or DEFAULT_FROM,
            reply_to=EMAIL_REPLY_TO,
        )
    except (EmailConfigError, EmailSendError) as err:
        log.error(f"PM link email failed for {email}: {err}")
        if raise_on_send_error:
            raise HTTPException(502, "Nie udało się wysłać emaila z linkiem.") from err
        return {"found": True, "sent": False, "account": account.value}

    log.info(f"PM update link sent to {email} (account: {account.value})")
    return {"found": True, "sent": True, "account": account.value}


# ── POST /payment-method/setup-intent (token HMAC) ───────────────────────────


async def create_setup_intent(*, token: str | None) -> dict:
    """Port ?action=create-intent: SetupIntent usage=off_session dla
    istniejacego customera. Zmiany vs oryginal: wymaga waznego tokenu HMAC
    (naprawa "zero autoryzacji") i szuka na OBU kontach (naprawa legacy)."""
    if not token or not isinstance(token, str):
        raise HTTPException(410, INVALID_TOKEN_MESSAGE)
    secret = _require_doi_secret()
    payload = magic_link.verify_token(token, secret)
    if payload is None or payload.get("purpose") != TOKEN_PURPOSE:
        raise HTTPException(410, INVALID_TOKEN_MESSAGE)
    email = normalize_email(str(payload["email"]))

    account, customer_id, any_customer = await _find_customer_with_relevant_sub(email)
    if account is None or customer_id is None:
        # Komunikaty 404 1:1 z oryginalem (brak konta vs brak suby).
        raise HTTPException(
            404, NO_SUBSCRIPTION_MESSAGE if any_customer else NO_ACCOUNT_MESSAGE
        )
    client = get_client(account)

    # SetupIntent usage=off_session = mandat SCA na odnowienia (1:1).
    setup_intent = await client.v1.setup_intents.create_async(
        params={
            "customer": customer_id,
            "payment_method_types": ["card"],
            "usage": "off_session",
            "metadata": {"purpose": TOKEN_PURPOSE, "customer_email": email},
        },
        options=request_options(idempotency_key=new_idempotency_key("pm-update")),
    )
    log.info(f"Created SetupIntent {setup_intent.id} for {email} ({account.value})")
    return {
        "clientSecret": setup_intent.client_secret,
        "setupIntentId": setup_intent.id,
        "account": account.value,
    }


# ── POST /payment-method/confirm ─────────────────────────────────────────────


async def confirm(*, setup_intent_id: str | None) -> dict:
    """Port ?action=confirm 1:1: default PM na customerze + wszystkich subach
    relevant + retry otwartych faktur (past_due/unpaid). Szuka SetupIntentu
    na OBU kontach (legacy). Wolane tez po powrocie z 3DS."""
    if not setup_intent_id or not isinstance(setup_intent_id, str):
        raise HTTPException(400, "Missing setupIntentId")

    setup_intent = None
    client = None
    for account in configured_accounts():
        candidate = get_client(account)
        try:
            setup_intent = await candidate.v1.setup_intents.retrieve_async(setup_intent_id)
            client = candidate
            break
        except stripe.InvalidRequestError:
            continue
    if setup_intent is None or client is None:
        raise HTTPException(404, MISSING_CARD_DATA_MESSAGE)

    status = obj_get(setup_intent, "status")
    if status != "succeeded":
        raise HTTPException(400, f"Karta nie została potwierdzona ({status}).")

    customer_id = _obj_id(obj_get(setup_intent, "customer"))
    payment_method_id = _obj_id(obj_get(setup_intent, "payment_method"))
    if not customer_id or not payment_method_id:
        raise HTTPException(400, MISSING_CARD_DATA_MESSAGE)

    await client.v1.customers.update_async(
        customer_id,
        params={"invoice_settings": {"default_payment_method": payment_method_id}},
    )

    subs = await client.v1.subscriptions.list_async(
        params={"customer": customer_id, "status": "all", "limit": 20}
    )
    relevant = [s for s in subs.data if obj_get(s, "status") in RELEVANT_STATUSES]

    updated_count = 0
    retried_invoice_ids: list[str] = []
    for sub in relevant:
        await client.v1.subscriptions.update_async(
            sub.id, params={"default_payment_method": payment_method_id}
        )
        updated_count += 1

        # Zalegla suba: natychmiastowa proba sciagniecia otwartych faktur
        # nowa karta; blad pojedynczej faktury logowany i polykany (1:1).
        if obj_get(sub, "status") in ("past_due", "unpaid"):
            open_invoices = await client.v1.invoices.list_async(
                params={
                    "customer": customer_id,
                    "subscription": sub.id,
                    "status": "open",
                    "limit": 5,
                }
            )
            for inv in open_invoices.data:
                try:
                    paid = await client.v1.invoices.pay_async(
                        inv.id, params={"payment_method": payment_method_id}
                    )
                    retried_invoice_ids.append(paid.id)
                    log.info(f"Retried invoice {paid.id}, status: {obj_get(paid, 'status')}")
                except Exception as err:
                    log.error(f"Failed to retry invoice {inv.id}: {err}")

    log.info(
        f"Updated {updated_count} subscriptions, retried {len(retried_invoice_ids)} invoices"
    )
    return {
        "success": True,
        "subscriptionsUpdated": updated_count,
        "invoicesRetried": len(retried_invoice_ids),
    }
