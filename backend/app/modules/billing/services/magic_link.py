"""Magic linki HMAC - wzor 1:1 z request-cancellation (port-kontrakt-2.md 1.3).

token = b64url(JSON(payload)) + "." + b64url(HMAC_SHA256(secret, b64url(JSON(payload))))
payload = {"email": <znormalizowany>, "exp": <epoch w MILISEKUNDACH>, ...ekstra}

- b64url bez paddingu, JSON bez spacji (jak JSON.stringify),
- weryfikacja: split po "." (dokladnie 2 czesci), HMAC przeliczony lokalnie,
  porownanie constant-time (hmac.compare_digest), potem parse payloadu
  i check exp vs teraz (ms).

Rejestr zuzytych tokenow (naprawa "tokenu wielokrotnego uzytku" z zadania
[billing-lifecycle]): token anulowania jest JEDNORAZOWY. Rejestr in-memory
(architektura zaklada jeden proces - jak rate limiter i WS broker fazy 1);
wpis zyje do exp tokenu, wiec po restarcie okno ponownego uzycia jest
ograniczone przez sam exp (60 min). Idempotentny po stronie STRIPE jest
skutek confirm (cancel_at_period_end) - wpis audytowy cancellation_reasons
juz NIE, dlatego claim() jest atomowy (check+mark bez await pomiedzy),
a release() oddaje token na sciezkach bez skutku (404/blad Stripe).
Tokeny zmiany karty pozostaja wielokrotnego uzytku w oknie exp
(kontrakt 1.3) - rejestru uzywa tylko anulowanie.
"""

import base64
import hashlib
import hmac
import json
import time

__all__ = [
    "sign_token",
    "verify_token",
    "claim",
    "release",
    "is_used",
    "mark_used",
    "reset_used",
]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def _now_ms() -> int:
    return int(time.time() * 1000)


def sign_token(payload: dict, secret: str) -> str:
    """Podpisuje payload HMAC-SHA256. JSON kompaktowy, klucze w kolejnosci
    wstawienia - 1:1 z JSON.stringify w oryginale."""
    data_b64 = _b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    sig = hmac.new(secret.encode(), data_b64.encode(), hashlib.sha256).digest()
    return f"{data_b64}.{_b64url(sig)}"


def verify_token(token: str, secret: str) -> dict | None:
    """Weryfikuje podpis i exp. None = token nieprawidlowy lub wygasly.

    Wymaga pol `email` i `exp` (ms) - 1:1 z verifyToken oryginalu.
    """
    parts = token.split(".")
    if len(parts) != 2:
        return None
    data_b64, sig_b64 = parts
    expected = hmac.new(secret.encode(), data_b64.encode(), hashlib.sha256).digest()
    try:
        given = _b64url_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(given, expected):
        return None
    try:
        payload = json.loads(_b64url_decode(data_b64))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not payload.get("email") or not isinstance(exp, int | float) or isinstance(exp, bool):
        return None
    if _now_ms() > exp:
        return None
    return payload


# ── Rejestr zuzytych tokenow (one-time use) ──────────────────────────────────

_used: dict[str, float] = {}  # sha256(token) -> exp_ms


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _prune() -> None:
    now = _now_ms()
    for key in [k for k, exp in _used.items() if exp < now]:
        _used.pop(key, None)


def is_used(token: str) -> bool:
    _prune()
    return _digest(token) in _used


def mark_used(token: str, *, exp_ms: float) -> None:
    _prune()
    _used[_digest(token)] = exp_ms


def claim(token: str, *, exp_ms: float) -> bool:
    """ATOMOWE zuzycie tokenu (sync, bez await - zero okna TOCTOU).

    False = token byl juz zuzyty. Wolaj PRZED operacjami na Stripe;
    na sciezkach bez skutku (404, blad Stripe) oddaj tokenem release().
    """
    _prune()
    key = _digest(token)
    if key in _used:
        return False
    _used[key] = exp_ms
    return True


def release(token: str) -> None:
    """Oddaje zaclaimowany token (confirm bez skutku - user moze powtorzyc)."""
    _used.pop(_digest(token), None)


def reset_used() -> None:
    """Czysci rejestr (testy)."""
    _used.clear()
