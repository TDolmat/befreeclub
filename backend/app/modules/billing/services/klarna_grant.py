"""WSPOLNA logika nadania dostepu czasowego Klarna: grant_one_time_access.

JEDNA implementacja zamiast trzech kopii oryginalu (confirm-klarna-checkout /
stripe-webhook / reconcile-klarna-checkouts - prowizorka #7 ze speca
billing-checkout.md). Uzywaja jej: confirm-klarna (services/checkout.py),
webhook handler [billing-webhook] i reconcile worker [workers].

Semantyka (port-kontrakt-2.md sekcja 3 + zadanie [billing-checkout]):
- refund-check: PELNY refund charge'a platnosci = odmowa (naprawa prowizorki
  #1 - zrefundowana sesja Klarny ma dalej payment_status=paid),
- expires_at = purchased_at + N miesiecy (kotwica = checkout_session.created,
  fallback teraz; arytmetyka miesiecy jak Date.setMonth w JS - przepelnienie
  dnia przechodzi na kolejny miesiac, np. 31.03 + 6 mies. -> 01.10).
  SWIADOME ODSTEPSTWO od "teraz + N" oryginalu (review 2.1): reconcile
  worker tyka co godzine i przy "teraz + N" kazdy tick podbijalby termin
  az sesja wypadnie z 7-dniowego okna (~7 dni gratis + ~168 eventow
  "extended" na czlonka). Kotwica daje identyczny termin z confirm,
  webhooka i reconcile. Podbicie expires_at TYLKO W GORE (max ze starym
  terminem) robi members.provision,
- mail powitalny RAZ: tylko gdy provision faktycznie aktywowal czlonka
  (nie already_active) i invite do Circle sie udal. Tresc 1:1 ze
  stripe-webhook (wersja ze zdaniem o ratach Klarny). Wysylka best-effort.
"""

import calendar
from datetime import UTC, datetime
from typing import Any

from app.core import email as email_core
from app.core.email import DEFAULT_FROM, normalize_email
from app.core.logging import create_logger
from app.core.stripe_client import StripeAccount, get_client
from app.modules.members.services import provisioning
from app.modules.members.services.provisioning import ProvisionResult

log = create_logger("billing.klarna")

# Dopelniacz pl-PL - format jak Date.toLocaleDateString("pl-PL",
# {day: "numeric", month: "long", year: "numeric"}), np. "10 czerwca 2026".
PL_MONTHS_GENITIVE = (
    "stycznia",
    "lutego",
    "marca",
    "kwietnia",
    "maja",
    "czerwca",
    "lipca",
    "sierpnia",
    "września",
    "października",
    "listopada",
    "grudnia",
)


class PaymentRefundedError(Exception):
    """Platnosc zostala w pelni zrefundowana - dostep NIE zostaje nadany."""


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    """Bezpieczny odczyt pola StripeObject (v15 nie jest dict, brak .get())."""
    if obj is None:
        return default
    try:
        return obj[key]
    except (KeyError, TypeError):
        return default


def add_months_js(dt: datetime, months: int) -> datetime:
    """Arytmetyka Date.setMonth z JS: dzien miesiaca zostaje, a gdy docelowy
    miesiac jest krotszy, nadmiar przelewa sie na poczatek kolejnego
    (31.03 + 6 -> 01.10). Zachowane 1:1 - od tego zaleza okna dostepu."""
    total = dt.month - 1 + months
    year = dt.year + total // 12
    month = total % 12 + 1
    day = dt.day
    days_in_month = calendar.monthrange(year, month)[1]
    if day > days_in_month:
        day -= days_in_month
        month += 1
        if month == 13:
            month = 1
            year += 1
    return dt.replace(year=year, month=month, day=day)


def format_pl_date(dt: datetime) -> str:
    return f"{dt.day} {PL_MONTHS_GENITIVE[dt.month - 1]} {dt.year}"


async def _charge_fully_refunded(payment_intent_id: str) -> bool:
    """Naprawa prowizorki #1: refundowana platnosc nie nadaje dostepu."""
    client = get_client(StripeAccount.CURRENT)
    pi = await client.v1.payment_intents.retrieve_async(
        payment_intent_id, params={"expand": ["latest_charge"]}
    )
    charge = _obj_get(pi, "latest_charge")
    if charge is None or isinstance(charge, str):
        return False
    return bool(_obj_get(charge, "refunded"))


async def send_klarna_welcome_email(email: str, plan_name: str, expires_at: datetime) -> None:
    """Mail powitalny Klarna - tresc 1:1 ze stripe-webhook/index.ts
    (sendKlarnaWelcomeEmail, wersja ze zdaniem o ratach). Best-effort."""
    if not email_core.is_configured():
        return
    expires = format_pl_date(expires_at)
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px; background: #ffffff; color: #1a1a1a;">
      <h1 style="font-size: 22px;">Witaj w Be Free Club! 🎉</h1>
      <p>Twoja płatność przez Klarna została zaakceptowana.</p>
      <p><strong>Plan:</strong> {plan_name}<br><strong>Dostęp aktywny do:</strong> {expires}</p>
      <p>Za chwilę dostaniesz osobne zaproszenie do społeczności Circle. Jeśli nie pojawi się w ciągu 5 minut, sprawdź spam lub napisz na <a href="mailto:krystian@befreeclub.pl">krystian@befreeclub.pl</a>.</p>
      <p>Pamiętaj: o płatności rat / przedłużenie terminu zajmie się Klarna. My po prostu otwieramy Ci drzwi do społeczności.</p>
      <p style="margin-top:32px;">Pozdrawiam,<br><strong>Krystian</strong><br>Be Free Club</p>
    </div>
  """
    try:
        await email_core.send_email(
            to=email,
            subject="Witaj w Be Free Club - dostęp aktywny ✅",
            html=html,
            from_email=DEFAULT_FROM,
            reply_to="krystian@befreeclub.pl",
        )
    except Exception as err:
        log.error(f"Klarna welcome email failed: {err}")


async def grant_one_time_access(
    *,
    email: str,
    duration_months: int,
    payment_intent_id: str | None,
    purchased_at: datetime | None = None,
) -> ProvisionResult:
    """Nadaje dostep czasowy po oplaconej platnosci jednorazowej (Klarna).

    purchased_at = checkout_session.created (kotwica terminu; None = teraz).
    Rzuca PaymentRefundedError gdy charge platnosci zostal w pelni
    zrefundowany. Idempotentne: czlonek juz aktywny dostaje tylko podbicie
    expires_at w gore (max - robi members.provision), bez maila i re-invite.
    """
    email = normalize_email(email)

    if payment_intent_id and await _charge_fully_refunded(payment_intent_id):
        log.warn(f"Klarna grant refused, payment refunded: {payment_intent_id} ({email})")
        raise PaymentRefundedError(payment_intent_id)

    expires_at = add_months_js(purchased_at or datetime.now(UTC), duration_months)
    result = await provisioning.provision(email, None, source="one_time", expires_at=expires_at)

    if not result.already_active and result.circle_invited:
        # planName 1:1 ze stripe-webhook (duration 12 -> "12 miesięcy", inaczej 6)
        plan_name = "12 miesięcy" if duration_months == 12 else "6 miesięcy"
        await send_klarna_welcome_email(email, plan_name, expires_at)

    log.info(
        f"Klarna grant: {email} invited={result.circle_invited} "
        f"already_active={result.already_active} expires={expires_at.isoformat()}"
    )
    return result
