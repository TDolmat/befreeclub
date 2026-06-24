"""Szyfrowanie symetryczne (Fernet) edytowalnych sekretow integracji.

Klucz master: settings.SECRETS_MASTER_KEY (44-znakowy url-safe base64, Fernet).
Lazy singleton - budujemy Fernet raz, przy pierwszym uzyciu.

ZASADA BEZPIECZENSTWA: brak albo zly klucz master NIGDY nie wywraca aplikacji.
- is_available() mowi, czy szyfrowanie dziala.
- encrypt() rzuca SecretBoxUnavailable, gdy klucz niedostepny (caller zamienia to
  na czytelny blad 400 - nie da sie zapisac sekretu bez dzialajacego szyfrowania).
- decrypt() przy braku klucza / uszkodzonym / cudzym tokenie zwraca None i loguje
  ostrzezenie RAZ (zeby nie zasypac logow), zamiast rzucac. Dzieki temu odczyt
  zawsze ma bezpieczny fallback na env.

Zero logowania wartosci sekretow - logujemy wylacznie fakt niedostepnosci klucza.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.logging import create_logger

log = create_logger("core.secret_box")


class SecretBoxUnavailable(RuntimeError):
    """Szyfrowanie niedostepne - brak albo niepoprawny SECRETS_MASTER_KEY."""


# Lazy singleton. _UNSET = jeszcze nie probowalismy zbudowac; None = probowalismy,
# klucz niedostepny/zly. Fernet = gotowy.
_UNSET = object()
_fernet: object = _UNSET
# Ostrzezenie o braku klucza logujemy raz na proces.
_warned = False


def _get_fernet() -> Fernet | None:
    global _fernet
    if _fernet is _UNSET:
        key = settings.SECRETS_MASTER_KEY
        if not key:
            _fernet = None
        else:
            try:
                _fernet = Fernet(key.encode("utf-8"))
            except (ValueError, TypeError):
                # Klucz w zlym formacie (nie 32-bajtowy url-safe base64).
                _fernet = None
    return _fernet  # type: ignore[return-value]


def _warn_once(msg: str) -> None:
    global _warned
    if not _warned:
        log.warn(msg)
        _warned = True


def reset_cache() -> None:
    """Czysci singleton Fernet i flage ostrzezenia (np. testy po monkeypatch klucza)."""
    global _fernet, _warned
    _fernet = _UNSET
    _warned = False


def is_available() -> bool:
    """True gdy SECRETS_MASTER_KEY jest obecny i poprawny (Fernet gotowy)."""
    return _get_fernet() is not None


def encrypt(plaintext: str) -> str:
    """Szyfruje plaintext do tokenu Fernet (str). Rzuca SecretBoxUnavailable gdy
    klucz master niedostepny/zly - bez dzialajacego szyfrowania nie zapisujemy
    sekretu (caller -> 400)."""
    fernet = _get_fernet()
    if fernet is None:
        raise SecretBoxUnavailable("SECRETS_MASTER_KEY niedostepny lub niepoprawny")
    return fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str | None:
    """Odszyfrowuje token Fernet. Zwraca None (nie rzuca) gdy:
    - klucz master niedostepny/zly,
    - token uszkodzony albo zaszyfrowany innym kluczem (InvalidToken),
    - token nie jest poprawnym stringiem.
    Loguje ostrzezenie raz. Wartosci NIE logujemy."""
    fernet = _get_fernet()
    if fernet is None:
        _warn_once("decrypt: SECRETS_MASTER_KEY niedostepny, zwracam None (fallback na env)")
        return None
    try:
        return fernet.decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        _warn_once("decrypt: token uszkodzony lub zaszyfrowany innym kluczem, zwracam None")
        return None
