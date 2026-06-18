"""Formularz kontaktowy (port send-contact-email + insert contact_messages).

Naprawa vs Supabase: INSERT do newsletter.contact_messages robi BACKEND
(koniec z insertem z przegladarki przez anon key i otwarty RLS). Mail do
Krystiana zostaje best-effort: blad wysylki jest LOGOWANY (nie cichy),
ale nie psuje requestu - zapis do DB wystarcza, jak w oryginale.
Brak RESEND_API_KEY = cichy sukces bez maila (1:1).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import email as email_core
from app.core.logging import create_logger
from app.modules.newsletter.models import ContactMessage

log = create_logger("newsletter:contact")

CONTACT_RECIPIENT = "krystian@befreeclub.pl"


def esc(value: str) -> str:
    """esc z send-contact-email (z apostrofem, inaczej niz escapeHtml DOI)."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def build_contact_email_html(name: str, email: str, message: str) -> str:
    """Tresc 1:1 z send-contact-email/index.ts (lacznie z wcieciami template literala)."""
    message_html = esc(message).replace("\n", "<br>")
    return f"""
          <h2>Nowa wiadomość z formularza kontaktowego</h2>
          <p><strong>Imię:</strong> {esc(name)}</p>
          <p><strong>Email:</strong> {esc(email)}</p>
          <p><strong>Wiadomość:</strong></p>
          <p>{message_html}</p>
        """


async def save_message(
    session: AsyncSession, *, name: str, email: str, message: str
) -> ContactMessage:
    row = ContactMessage(name=name, email=email, message=message)
    session.add(row)
    await session.commit()
    return row


async def send_notification_email(*, name: str, email: str, message: str) -> None:
    """Best-effort mail do Krystiana. Nigdy nie rzuca - bledy sa logowane."""
    if not email_core.is_configured():
        log.info("RESEND_API_KEY not set, skipping contact email send")
        return
    try:
        await email_core.send_email(
            to=CONTACT_RECIPIENT,
            subject=f"Nowa wiadomość od {esc(name)[:80]}",
            html=build_contact_email_html(name, email, message),
            reply_to=email,
        )
    except (email_core.EmailConfigError, email_core.EmailSendError) as err:
        log.error(f"contact email send failed (message saved in DB): {err}")
