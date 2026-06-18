"""Port newsletter-subscribe / newsletter-confirm / send-contact-email.

Montowane pod /api/newsletter (publiczne; GET /contact-messages za
require_auth per-route, bo router jest publiczny).
  POST /subscribe         - stateless DOI: walidacja, token HMAC 14 dni,
                            mail potwierdzajacy przez Resend (tresc 1:1)
  POST /confirm           - weryfikacja tokenu, push do Sender.net,
                            Meta CAPI Lead (event_id deterministyczny)
  POST /contact           - INSERT newsletter.contact_messages przez backend
                            + best-effort mail do Krystiana
  GET  /contact-messages  - lista dla panelu admina (paginacja limit/offset)

Rate limit (port-kontrakt-2 sekcja 1.4, polityka RL-mail 5/15min -> lock 1h):
subscribe per IP+email, contact per IP. Confirm bez limitu (auth = token HMAC).
"""

import hashlib
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import meta_capi
from app.core.config import settings
from app.core.db import get_session
from app.core.email import (
    EmailConfigError,
    EmailSendError,
    normalize_email,
    send_email,
)
from app.core.email import (
    is_configured as email_is_configured,
)
from app.core.logging import create_logger
from app.core.schemas import dump
from app.modules.admin.services.auth import require_auth
from app.modules.admin.services.rate_limit import client_ip, is_locked, record_failure
from app.modules.newsletter.models import ContactMessage
from app.modules.newsletter.schemas import (
    SIMPLE_EMAIL_RE,
    ConfirmIn,
    ContactIn,
    ContactMessageOut,
    SubscribeIn,
)
from app.modules.newsletter.services import contact as contact_service
from app.modules.newsletter.services import doi, sender

log = create_logger("newsletter")

router = APIRouter()

DEFAULT_CONFIRM_URL_BASE = "https://befreeclub.pl/newsletter/potwierdz"
DEFAULT_NEWSLETTER_FROM = "Be Free Club <krystian@befreeclub.pl>"
NEWSLETTER_REPLY_TO = "krystian@befreeclub.pl"
RATE_LIMIT_MESSAGE = "Zbyt wiele prób. Spróbuj ponownie później."


def lead_event_id(email: str) -> str:
    """event_id Lead wg kontraktu 5.2: sha256(email + ":" + YYYY-MM-DD) w UTC.
    Deterministyczny - front uzywa tego samego id w pixelu (deduplikacja)."""
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return hashlib.sha256(f"{normalize_email(email)}:{day}".encode()).hexdigest()


def _capi_client_ip(request: Request) -> str | None:
    ip = client_ip(request)
    return None if ip == "unknown" else ip


@router.post("/subscribe")
async def subscribe(payload: SubscribeIn, request: Request) -> dict:
    name = payload.name.strip()
    email = normalize_email(payload.email)

    key = f"newsletter-subscribe|{client_ip(request)}|{email}"
    if is_locked(key)["locked"]:
        raise HTTPException(429, RATE_LIMIT_MESSAGE)
    record_failure(key)

    # Oryginal: throw przed walidacja gdy brak sekretow -> 500 "Internal error".
    # email_is_configured liczy tez mock dev (mail laduje w .dev-outbox/).
    if not email_is_configured() or settings.NEWSLETTER_DOI_SECRET is None:
        log.error("newsletter-subscribe misconfigured (RESEND_API_KEY / NEWSLETTER_DOI_SECRET)")
        raise HTTPException(500, "Internal error")

    if not name or len(name) > 80:
        raise HTTPException(400, "Niepoprawne imię")
    if not SIMPLE_EMAIL_RE.match(email) or len(email) > 255:
        raise HTTPException(400, "Niepoprawny email")

    exp = doi.now_ms() + doi.DOI_TOKEN_TTL_MS
    token = doi.sign_token(
        {"email": email, "name": name, "exp": exp}, settings.NEWSLETTER_DOI_SECRET
    )
    confirm_base = settings.CONFIRM_URL_BASE or DEFAULT_CONFIRM_URL_BASE
    confirm_url = f"{confirm_base}?token={quote(token, safe='')}"
    attempt_id = str(uuid.uuid4())
    sent_at = doi.sent_at_label()

    try:
        await send_email(
            to=email,
            subject=f"{name} potwierdź swój zapis - nowy link {sent_at}",
            html=doi.build_confirm_email_html(name, confirm_url, sent_at),
            from_email=settings.NEWSLETTER_FROM_EMAIL or DEFAULT_NEWSLETTER_FROM,
            reply_to=NEWSLETTER_REPLY_TO,
            headers={"X-Entity-Ref-ID": attempt_id},
        )
    except EmailConfigError as err:
        raise HTTPException(500, "Internal error") from err
    except EmailSendError as err:
        log.error(f"Resend error: {err.status} {(err.body or '')[:300]}")
        raise HTTPException(502, "Nie udało się wysłać maila potwierdzającego") from err

    log.info(f"Resend OK to: {email} attempt: {attempt_id}")
    return {"ok": True}


@router.post("/confirm")
async def confirm(payload: ConfirmIn, request: Request) -> dict:
    if settings.NEWSLETTER_DOI_SECRET is None or not sender.is_configured():
        log.error("newsletter-confirm misconfigured (NEWSLETTER_DOI_SECRET / SENDER_API_TOKEN)")
        raise HTTPException(500, "Internal error")

    if not payload.token:
        raise HTTPException(400, "Brak tokenu")

    data = doi.verify_token(payload.token, settings.NEWSLETTER_DOI_SECRET)
    if data is None:
        raise HTTPException(400, "Link wygasł lub jest nieprawidłowy")

    email = normalize_email(data["email"])
    name = str(data.get("name") or "").strip()

    ok = await sender.push_subscriber(email, name)
    if not ok:
        raise HTTPException(502, "Nie udało się dokończyć zapisu. Spróbuj za chwilę.")

    # Meta CAPI Lead - jedyny moment potwierdzonej konwersji newslettera
    # (kontrakt 5.3). send_event nigdy nie rzuca; brak konfiguracji = no-op.
    event_id = lead_event_id(email)
    await meta_capi.send_event(
        event_name="Lead",
        event_id=event_id,
        event_time=int(time.time()),
        user_data=meta_capi.CapiUserData(
            email=email,
            client_ip=_capi_client_ip(request),
            client_ua=request.headers.get("user-agent"),
        ),
        event_source_url=settings.FRONTEND_URL or "https://befreeclub.pl",
    )

    return {"ok": True, "name": name, "eventId": event_id}


@router.post("/contact")
async def contact(
    payload: ContactIn,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict:
    key = f"newsletter-contact|{client_ip(request)}"
    if is_locked(key)["locked"]:
        raise HTTPException(429, RATE_LIMIT_MESSAGE)
    record_failure(key)

    name = payload.name.strip()
    email = payload.email.strip()
    message = payload.message.strip()
    if (
        not name
        or len(name) > 100
        or not email
        or len(email) > 255
        or not SIMPLE_EMAIL_RE.match(email)
        or not message
        or len(message) > 5000
    ):
        raise HTTPException(400, "Invalid input")

    email = normalize_email(email)
    await contact_service.save_message(db, name=name, email=email, message=message)
    await contact_service.send_notification_email(name=name, email=email, message=message)
    return {"ok": True}


@router.get("/contact-messages", dependencies=[Depends(require_auth)])
async def list_contact_messages(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_session),
) -> dict:
    total = (await db.execute(select(func.count()).select_from(ContactMessage))).scalar_one()
    rows = (
        (
            await db.execute(
                select(ContactMessage)
                .order_by(ContactMessage.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "messages": [dump(ContactMessageOut.model_validate(row)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
