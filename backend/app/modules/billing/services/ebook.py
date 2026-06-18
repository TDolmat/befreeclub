"""Serwis ebooka "Na swoich zasadach jako freelancer" (port flow A, webhook-first).

KONTRAKT dla [billing-webhook] (port-kontrakt-2.md wiersz 8):
- `fulfill_ebook_order(payment_intent)` - IDEMPOTENTNY fulfillment wolany
  z dwoch miejsc: POST /api/billing/ebook/confirm (przyspieszacz UX) oraz
  handler webhooka `payment_intent.succeeded` z metadata.product == "ebook"
  (gwarancja fulfillmentu bez powrotu przegladarki).
- `invalidate_ebook_tokens(email=... | payment_intent_id=...)` - dla handlera
  `charge.refunded` (naprawa #3: refund uniewaznia tokeny pobrania). Oprocz
  revoked_at na tokenach ustawia tez status `refunded` na pasujacych orderach.

Sesje: kazda funkcja przyjmuje opcjonalna sesje. Z przekazana sesja robi tylko
flush (caller commituje); bez sesji otwiera wlasna i commituje sama.

Idempotencja fulfillmentu: INSERT ... ON CONFLICT (stripe_payment_intent_id)
DO NOTHING + SELECT ... FOR UPDATE. Lock na orderze trzymany takze przez
wysylke maila - rownolegly confirm/webhook czeka i widzi email_sent_at,
wiec mail idzie dokladnie RAZ (guard 1:1 z oryginalem, wyscig naprawiony).
"""

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import async_session_maker
from app.core.email import EmailConfigError, EmailSendError, normalize_email, send_email
from app.core.logging import create_logger
from app.modules.billing.models import EbookDownloadToken, EbookOrder

log = create_logger("billing:ebook")

EBOOK_PLAN_SLUG = "ebook"
# Opis PI i tresci 1:1 z create-ebook-payment-intent / confirm-ebook-purchase.
EBOOK_DESCRIPTION = "Ebook: Na swoich zasadach jako freelancer"
EBOOK_DOWNLOAD_FILENAME = "Na-swoich-zasadach-jako-freelancer.pdf"
EBOOK_EMAIL_SUBJECT = "Twój ebook jest gotowy do pobrania 📘"
TOKEN_VALID_DAYS = 30
FRONTEND_URL_DEFAULT = "https://befreeclub.pl"

_NIP_CLEAN_RE = re.compile(r"[\s-]")


def _email_html(download_url: str) -> str:
    """HTML maila z linkiem pobrania - tresc i design 1:1 z confirm-ebook-purchase."""
    return f"""
    <div style="font-family:-apple-system,Segoe UI,Inter,sans-serif;background:#f7f5f0;padding:32px 16px;color:#2d2f33">
      <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:16px;padding:32px;border:1px solid #e8e4d8">
        <h1 style="margin:0 0 12px;font-size:24px;color:#2d2f33">Dzięki za zakup! 🎉</h1>
        <p style="font-size:16px;line-height:1.6;margin:0 0 20px">Twój ebook <strong>"Na swoich zasadach jako freelancer"</strong> jest gotowy do pobrania.</p>
        <a href="{download_url}" style="display:inline-block;background:#e8b54a;color:#2d2f33;padding:14px 28px;border-radius:10px;font-weight:700;text-decoration:none;font-size:16px">📘 Pobierz ebooka (PDF)</a>
        <p style="font-size:14px;color:#6b6b6b;margin:24px 0 8px">Link jest aktywny przez 30 dni i pozwala pobrać plik do 10 razy. Zapisz PDF na dysku po pobraniu.</p>
        <p style="font-size:14px;color:#6b6b6b;margin:0">W razie problemów napisz na <a href="mailto:krystian@befreeclub.pl" style="color:#2d2f33">krystian@befreeclub.pl</a></p>
        <hr style="border:none;border-top:1px solid #e8e4d8;margin:24px 0" />
        <p style="font-size:12px;color:#999;margin:0">Be Free Club · befreeclub.pl</p>
      </div>
    </div>
  """


def download_url_for(token: str) -> str:
    base = settings.FRONTEND_URL or FRONTEND_URL_DEFAULT
    return f"{base}/ebook/pobierz?token={token}"


class EbookFulfillmentError(Exception):
    """Fulfillment niemozliwy (brak emaila, zly status PI, order zrefundowany).

    status: kod HTTP dla endpointu confirm (500 jak oryginalny throw,
    409 dla orderu po refundzie).
    """

    def __init__(self, message: str, *, status: int = 500):
        super().__init__(message)
        self.status = status


class DownloadTokenError(Exception):
    """Walidacja tokenu pobrania nie przeszla - status + komunikat PL 1:1."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class FulfillmentResult:
    order_id: UUID
    email: str
    token: str
    download_url: str
    email_sent: bool  # czy mail wyszedl w TYM wywolaniu


@dataclass(frozen=True)
class ConsumedDownload:
    file_path: str
    filename: str
    remaining_downloads: int


async def fulfill_ebook_order(
    payment_intent: dict[str, Any],
    *,
    email: str | None = None,
    wants_invoice: bool = False,
    nip: str | None = None,
    invoice_name: str | None = None,
    session: AsyncSession | None = None,
) -> FulfillmentResult:
    """Idempotentny fulfillment zamowienia ebooka dla succeeded PaymentIntenta.

    payment_intent: surowy PI - dict z payloadu webhooka (event.data.object)
    albo obiekt SDK (konwertowany przez to_dict()). Zadnych wywolan Stripe
    w srodku - caller odpowiada za zrodlo PI (confirm: retrieve + check
    statusu; webhook: event).

    Semantyka 1:1 z confirm-ebook-purchase: email klienta ma pierwszenstwo
    przed pi.receipt_email; NIP czyszczony z [\\s-]; reuse waznego tokenu
    (30 dni); mail tylko raz (guard email_sent_at), blad maila NIE psuje
    fulfillmentu (mail ponowi sie przy nastepnym wywolaniu).

    Naprawy portu: upsert po UNIQUE stripe_payment_intent_id (dlug #10),
    dane fakturowe takze z metadata PI (webhook-first dostaje je bez powrotu
    przegladarki), order `refunded` nigdy nie wraca do `paid`.
    """
    if session is not None:
        return await _fulfill(session, payment_intent, email, wants_invoice, nip, invoice_name)
    async with async_session_maker() as own:
        result = await _fulfill(own, payment_intent, email, wants_invoice, nip, invoice_name)
        await own.commit()
        return result


async def _fulfill(
    session: AsyncSession,
    payment_intent: dict[str, Any],
    email: str | None,
    wants_invoice: bool,
    nip: str | None,
    invoice_name: str | None,
) -> FulfillmentResult:
    if hasattr(payment_intent, "to_dict"):
        # StripeObject z SDK (stripe>=15 nie jest dictem) -> plain dict;
        # payload webhooka juz jest dictem.
        payment_intent = payment_intent.to_dict()
    pi_id = payment_intent.get("id")
    if not pi_id:
        raise EbookFulfillmentError("Missing payment intent id")
    status = payment_intent.get("status")
    if status != "succeeded":
        raise EbookFulfillmentError(f"Payment status: {status}")

    resolved_email = normalize_email(str(email or payment_intent.get("receipt_email") or ""))
    if not resolved_email:
        raise EbookFulfillmentError("Missing email")

    # Dane fakturowe: body confirm ma pierwszenstwo, fallback metadata PI
    # (create zapisuje je tam, zeby webhook-first mial je bez przegladarki).
    metadata = payment_intent.get("metadata") or {}
    wants_invoice = wants_invoice or metadata.get("wants_invoice") == "true"
    nip = nip or metadata.get("nip") or None
    invoice_name = invoice_name or metadata.get("invoice_name") or None
    nip_clean = _NIP_CLEAN_RE.sub("", str(nip)) if nip else None

    now = datetime.now(UTC)
    await session.execute(
        pg_insert(EbookOrder)
        .values(
            email=resolved_email,
            stripe_payment_intent_id=pi_id,
            status="paid",
            amount_paid=int(payment_intent.get("amount") or 0),
            currency=str(payment_intent.get("currency") or "pln"),
            paid_at=now,
            wants_invoice=wants_invoice,
            nip=nip_clean if wants_invoice else None,
            invoice_name=invoice_name if wants_invoice else None,
        )
        .on_conflict_do_nothing(index_elements=["stripe_payment_intent_id"])
    )
    # FOR UPDATE serializuje rownolegle fulfillmenty tego samego PI
    # (confirm retry 8x2s + webhook) - drugi czeka i widzi efekty pierwszego.
    order = (
        await session.execute(
            select(EbookOrder)
            .where(EbookOrder.stripe_payment_intent_id == pi_id)
            .with_for_update()
        )
    ).scalar_one()

    if order.status == "refunded":
        # Refund nie moze wrocic do paid - kupujacy po zwrocie nie dostaje
        # nowego linku (naprawa #3, brak odpowiednika w oryginale).
        raise EbookFulfillmentError("Order refunded", status=409)

    if order.status != "paid" or order.email != resolved_email:
        order.status = "paid"
        order.email = resolved_email
        order.paid_at = now
    if wants_invoice:
        order.wants_invoice = True
        if nip_clean:
            order.nip = nip_clean
        if invoice_name:
            order.invoice_name = invoice_name

    token_row = (
        await session.execute(
            select(EbookDownloadToken)
            .where(
                EbookDownloadToken.order_id == order.id,
                EbookDownloadToken.expires_at > now,
                EbookDownloadToken.revoked_at.is_(None),
            )
            .order_by(EbookDownloadToken.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if token_row is not None:
        token = token_row.token
    else:
        token = secrets.token_hex(32)  # 64 znaki hex, jak oryginal
        session.add(
            EbookDownloadToken(
                order_id=order.id,
                token=token,
                email=resolved_email,
                expires_at=now + timedelta(days=TOKEN_VALID_DAYS),
            )
        )

    download_url = download_url_for(token)

    email_sent = False
    if order.email_sent_at is None:
        try:
            await send_email(
                to=resolved_email, subject=EBOOK_EMAIL_SUBJECT, html=_email_html(download_url)
            )
        except (EmailConfigError, EmailSendError) as err:
            # 1:1 z oryginalem: blad Resend tylko logujemy, response success;
            # email_sent_at zostaje NULL, wiec nastepne wywolanie ponowi mail.
            log.error(f"ebook fulfillment mail failed for {pi_id}: {err}")
        else:
            order.email_sent_at = datetime.now(UTC)
            email_sent = True

    await session.flush()
    return FulfillmentResult(
        order_id=order.id,
        email=resolved_email,
        token=token,
        download_url=download_url,
        email_sent=email_sent,
    )


async def invalidate_ebook_tokens(
    *,
    email: str | None = None,
    payment_intent_id: str | None = None,
    session: AsyncSession | None = None,
) -> int:
    """Uniewaznia tokeny pobrania po refundzie (naprawa #3). Zwraca liczbe
    uniewaznionych tokenow.

    Kryterium: email (znormalizowany) LUB payment_intent_id - podaj minimum
    jedno. Oprocz revoked_at na tokenach ustawia status `refunded` na
    pasujacych orderach ze statusem `paid`. Idempotentne (drugi raz -> 0).
    """
    if email is None and payment_intent_id is None:
        raise ValueError("email or payment_intent_id is required")
    if session is not None:
        return await _invalidate(session, email, payment_intent_id)
    async with async_session_maker() as own:
        revoked = await _invalidate(own, email, payment_intent_id)
        await own.commit()
        return revoked


async def _invalidate(
    session: AsyncSession, email: str | None, payment_intent_id: str | None
) -> int:
    normalized = normalize_email(email) if email else None
    order_criteria = []
    if normalized:
        order_criteria.append(EbookOrder.email == normalized)
    if payment_intent_id:
        order_criteria.append(EbookOrder.stripe_payment_intent_id == payment_intent_id)
    order_filter = or_(*order_criteria)

    token_filter = EbookDownloadToken.order_id.in_(select(EbookOrder.id).where(order_filter))
    if normalized:
        # Pas i szelki: tokeny nosza wlasna kopie emaila z momentu utworzenia.
        token_filter = or_(token_filter, EbookDownloadToken.email == normalized)

    now = datetime.now(UTC)
    revoked_ids = (
        (
            await session.execute(
                update(EbookDownloadToken)
                .where(EbookDownloadToken.revoked_at.is_(None), token_filter)
                .values(revoked_at=now)
                .returning(EbookDownloadToken.id)
            )
        )
        .scalars()
        .all()
    )
    await session.execute(
        update(EbookOrder)
        .where(order_filter, EbookOrder.status == "paid")
        .values(status="refunded")
    )
    await session.flush()
    if revoked_ids:
        log.info(
            f"revoked {len(revoked_ids)} ebook download token(s)",
            {"email": normalized, "payment_intent_id": payment_intent_id},
        )
    return len(revoked_ids)


async def record_refund_tombstone(
    *,
    payment_intent_id: str,
    email: str,
    amount: int,
    currency: str = "pln",
    session: AsyncSession | None = None,
) -> bool:
    """Tombstone `refunded` gdy charge.refunded wyprzedzil fulfillment.

    Stripe nie gwarantuje kolejnosci eventow: refund tuz po platnosci moze
    przyjsc PRZED payment_intent.succeeded. Bez wiersza orderu invalidate
    nie ma czego uniewaznic, a pozniejszy fulfillment wydalby ebooka po
    zwrocie (PI po refundzie dalej ma status succeeded). Tombstone ze
    statusem `refunded` blokuje to przez istniejacy guard "refunded nigdy
    nie wraca do paid" w _fulfill. ON CONFLICT DO NOTHING: gdy order juz
    istnieje, status ustawil invalidate. Zwraca True gdy tombstone powstal.
    """
    normalized = normalize_email(email)
    if not normalized:
        return False
    if session is not None:
        return await _record_tombstone(session, payment_intent_id, normalized, amount, currency)
    async with async_session_maker() as own:
        created = await _record_tombstone(own, payment_intent_id, normalized, amount, currency)
        await own.commit()
        return created


async def _record_tombstone(
    session: AsyncSession, payment_intent_id: str, email: str, amount: int, currency: str
) -> bool:
    inserted = (
        await session.execute(
            pg_insert(EbookOrder)
            .values(
                email=email,
                stripe_payment_intent_id=payment_intent_id,
                status="refunded",
                amount_paid=amount,
                currency=currency or "pln",
            )
            .on_conflict_do_nothing(index_elements=["stripe_payment_intent_id"])
            .returning(EbookOrder.id)
        )
    ).scalar_one_or_none()
    await session.flush()
    if inserted is not None:
        log.info(f"refund tombstone created for {payment_intent_id} ({email})")
    return inserted is not None


async def consume_download_token(
    token: str, *, session: AsyncSession | None = None
) -> ConsumedDownload:
    """Walidacja tokenu + ATOMOWE zuzycie pobrania (naprawa dlugu #9).

    Rzuca DownloadTokenError z kodem i komunikatem PL 1:1:
    404 "Nieprawidłowy link", 410 wygasly/uniewazniony, 429 limit,
    500 brak pliku na dysku. Licznik: UPDATE ... WHERE download_count <
    max_downloads RETURNING - rownolegle requesty nie przekrocza limitu.
    """
    if session is not None:
        return await _consume(session, token)
    async with async_session_maker() as own:
        result = await _consume(own, token)
        await own.commit()
        return result


async def _consume(session: AsyncSession, token: str) -> ConsumedDownload:
    row = (
        await session.execute(select(EbookDownloadToken).where(EbookDownloadToken.token == token))
    ).scalar_one_or_none()
    if row is None:
        raise DownloadTokenError(404, "Nieprawidłowy link")

    now = datetime.now(UTC)
    if row.revoked_at is not None or row.expires_at < now:
        # Token po refundzie komunikacyjnie = wygasly (oryginal nie mial revoke).
        raise DownloadTokenError(410, "Link wygasł. Napisz na krystian@befreeclub.pl po nowy.")
    if row.download_count >= row.max_downloads:
        raise DownloadTokenError(429, "Limit pobrań wyczerpany. Napisz na krystian@befreeclub.pl.")

    # Jak oryginal: najpierw dostepnosc pliku (signed URL), licznik dopiero
    # po sukcesie - blad pliku nie zuzywa pobrania.
    file_path = settings.EBOOK_FILE_PATH
    if not file_path or not Path(file_path).is_file():
        log.error(f"EBOOK_FILE_PATH missing or not a file: {file_path!r}")
        raise DownloadTokenError(500, "Plik tymczasowo niedostępny. Spróbuj za chwilę.")

    consumed = (
        await session.execute(
            update(EbookDownloadToken)
            .where(
                EbookDownloadToken.id == row.id,
                EbookDownloadToken.download_count < EbookDownloadToken.max_downloads,
                EbookDownloadToken.revoked_at.is_(None),
            )
            .values(
                download_count=EbookDownloadToken.download_count + 1,
                last_downloaded_at=now,
            )
            .returning(EbookDownloadToken.download_count, EbookDownloadToken.max_downloads)
        )
    ).first()
    if consumed is None:
        # Przegrany wyscig o ostatnie pobranie (albo rownolegly revoke).
        raise DownloadTokenError(429, "Limit pobrań wyczerpany. Napisz na krystian@befreeclub.pl.")

    download_count, max_downloads = consumed
    await session.flush()
    return ConsumedDownload(
        file_path=file_path,
        filename=EBOOK_DOWNLOAD_FILENAME,
        remaining_downloads=max_downloads - download_count,
    )
