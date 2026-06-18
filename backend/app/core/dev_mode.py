"""Tryb lokalny: auto-mock zewnetrznych serwisow w dev.

Konwencja: gdy NODE_ENV != production i brak klucza danego serwisu, serwis
przechodzi w tryb mock z JEDNYM WARN przy starcie (zamiast 500/wyjatkow).
Jawne flagi MOCK_EMAIL / MOCK_SENDER / MOCK_CIRCLE_MEMBERS (puste = auto)
wymuszaja mock nawet z kluczem; false wylacza mock na sile (uzywaja testy).
Na produkcji NIC sie nie mockuje - brak klucza to twardy blad jak dotychczas.

Co NIE jest mockowane nigdy:
- Stripe: dev uzywa kluczy testowych sk_test_ (guard sk_live_ w config.py
  odmawia startu poza produkcja),
- klient Circle w module circle_dm: dev gada z realnym Circle na kontach
  testowych (konwencja projektu).
"""

from app.core.config import settings
from app.core.logging import create_logger

log = create_logger("dev-mode")


def is_production() -> bool:
    return settings.NODE_ENV == "production"


def resolve_mock(flag: bool | None, has_key: bool) -> bool:
    """Decyzja mocka: prod nigdy; jawna flaga wygrywa; auto = brak klucza."""
    if is_production():
        return False
    if flag is not None:
        return flag
    return not has_key


def _reason(flag: bool | None, flag_name: str, missing: str) -> str:
    return f"wymuszony {flag_name}=true" if flag else f"brak {missing}"


def log_startup_mode() -> None:
    """Jeden WARN per zmockowany/wylaczony serwis - wolane raz z lifespan.

    Importy lazy, zeby core nie zalezalo od modulow przy imporcie.
    """
    from app.core import email, meta_capi
    from app.modules.members.services import circle
    from app.modules.newsletter.services import sender

    if is_production():
        if not meta_capi.is_configured():
            log.info("Meta CAPI disabled (missing META_CAPI_TOKEN / META_PIXEL_ID)")
        return

    if email.is_mocked():
        log.warn(
            f"[MOCK] Email/Resend ({_reason(settings.MOCK_EMAIL, 'MOCK_EMAIL', 'RESEND_API_KEY')})"
            ": maile beda zapisywane jako pliki HTML w backend/.dev-outbox/"
        )
    if sender.is_mocked():
        log.warn(
            f"[MOCK] Sender.net ({_reason(settings.MOCK_SENDER, 'MOCK_SENDER', 'SENDER_API_TOKEN')})"
            ": push subskrybentow tylko logowany, zwraca sukces"
        )
    if circle.is_mocked():
        log.warn(
            "[MOCK] Circle members "
            f"({_reason(settings.MOCK_CIRCLE_MEMBERS, 'MOCK_CIRCLE_MEMBERS', 'CIRCLE_API_TOKEN/CIRCLE_COMMUNITY_ID')})"
            ": invite/remove/find na fake'u in-memory; circle_dm NIE jest mockowane"
        )
    if not meta_capi.is_configured():
        log.warn(
            "Meta CAPI wylaczony (brak META_PIXEL_ID / META_CAPI_TOKEN)"
            " - eventy nie beda wysylane"
        )
