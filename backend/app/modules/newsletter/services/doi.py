"""Stateless double opt-in newslettera (port newsletter-subscribe/confirm).

Token = b64url(JSON payloadu bez spacji) + "." + b64url(HMAC_SHA256(secret, b64url))
- format 1:1 z edge functions (wlasny format, nie JWT), sekret NEWSLETTER_DOI_SECRET,
exp w epoch MILISEKUNDACH (+14 dni). Tokeny sa wielokrotnego uzytku w oknie exp.
Weryfikacja podpisu constant-time (hmac.compare_digest).

Tu tez zyje szablon maila DOI (tresc PL bajt w bajt z oryginalu) i label
timestampu pl-PL Europe/Warsaw do tematu maila.
"""

import base64
import binascii
import hashlib
import hmac
import json
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

DOI_TOKEN_TTL_MS = 14 * 24 * 60 * 60 * 1000

_WARSAW = ZoneInfo("Europe/Warsaw")


def now_ms() -> int:
    """Date.now() - epoch w milisekundach."""
    return int(time.time() * 1000)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded)


def _hmac_sha256(secret: str, data: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).digest()


def sign_token(payload: dict[str, Any], secret: str) -> str:
    """JSON bez spacji (jak JSON.stringify), klucze w kolejnosci wstawienia."""
    json_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    data_b64 = _b64url_encode(json_str.encode("utf-8"))
    sig = _hmac_sha256(secret, data_b64)
    return f"{data_b64}.{_b64url_encode(sig)}"


def verify_token(token: str, secret: str) -> dict[str, Any] | None:
    """Zwraca payload albo None (zly format / zly podpis / wygasly / brak pol).

    Kolejnosc jak w oryginale: najpierw podpis (constant-time), dopiero potem
    parse payloadu i check exp. Jeden wspolny wynik None dla wszystkich bledow.
    """
    parts = token.split(".")
    if len(parts) != 2:
        return None
    data_b64, sig_b64 = parts
    expected_sig = _hmac_sha256(secret, data_b64)
    try:
        given_sig = _b64url_decode(sig_b64)
    except (binascii.Error, ValueError):
        return None
    if not hmac.compare_digest(given_sig, expected_sig):
        return None
    try:
        payload = json.loads(_b64url_decode(data_b64))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    email = payload.get("email")
    exp = payload.get("exp")
    # JS: !payload.email || !payload.exp (puste/0 = nieprawidlowy).
    if not email or not exp:
        return None
    if not isinstance(email, str) or not isinstance(exp, int | float):
        return None
    if now_ms() > exp:
        return None
    return payload


def sent_at_label(now: datetime | None = None) -> str:
    """Intl.DateTimeFormat("pl-PL", {dateStyle:"short", timeStyle:"medium",
    timeZone:"Europe/Warsaw"}) -> "3.06.2026, 08:05:09".

    CLDR pl short = d.MM.y: dzien BEZ zera wiodacego, miesiac/godzina/minuty/
    sekundy padowane (zweryfikowane formatToParts w V8/ICU)."""
    dt = (now or datetime.now(tz=_WARSAW)).astimezone(_WARSAW)
    return (
        f"{dt.day}.{dt.month:02d}.{dt.year}, {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    )


def escape_html(value: str) -> str:
    """escapeHtml z newsletter-subscribe (bez apostrofu - 1:1 z oryginalem)."""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


# Tresc 1:1 z buildEmailHtml w newsletter-subscribe/index.ts.
_CONFIRM_EMAIL_TEMPLATE = """<!DOCTYPE html>
<html lang="pl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<title>Potwierdź zapis</title>
<style>
  :root { color-scheme: light only; supported-color-schemes: light only; }
  @media (prefers-color-scheme: dark) {
    body, table, td, p, h1, a, span, div { color-scheme: light only !important; }
    .bfc-card { background:#2c2d31 !important; }
    .bfc-bg { background:#1a1b1f !important; }
    .bfc-btn { background:#ECE183 !important; }
    .bfc-btn-link { color:#1a1b1f !important; }
    .bfc-h1 { color:#ffffff !important; }
    .bfc-body { color:#cfd1d4 !important; }
    .bfc-muted { color:#888 !important; }
    .bfc-faint { color:#666 !important; }
    .bfc-eyebrow { color:#999 !important; }
    .bfc-brand { color:#ECE183 !important; }
  }
  [data-ogsc] .bfc-card { background:#2c2d31 !important; }
  [data-ogsc] .bfc-bg { background:#1a1b1f !important; }
  [data-ogsc] .bfc-btn { background:#ECE183 !important; }
  [data-ogsc] .bfc-btn-link { color:#1a1b1f !important; }
  [data-ogsc] .bfc-h1 { color:#ffffff !important; }
  [data-ogsc] .bfc-body { color:#cfd1d4 !important; }
  [data-ogsc] .bfc-brand { color:#ECE183 !important; }
</style>
</head>
<body class="bfc-bg" style="margin:0;padding:0;background:#1a1b1f;font-family:'Space Grotesk','Inter',Arial,sans-serif;-webkit-text-size-adjust:100%;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="bfc-bg" style="background:#1a1b1f;">
    <tr><td align="center" style="padding:40px 16px;">
      <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" class="bfc-card" style="max-width:560px;width:100%;background:#2c2d31;border-radius:16px;border:1px solid #3a3b3f;">
        <tr><td style="padding:40px 32px;">
          <p class="bfc-eyebrow" style="font-size:12px;color:#999;letter-spacing:1px;text-transform:uppercase;margin:0 0 20px;font-family:Arial,sans-serif;">Newsletter <span class="bfc-brand" style="color:#ECE183;">Be Free Club</span></p>
          <h1 class="bfc-h1" style="font-size:28px;font-weight:700;line-height:1.2;color:#ffffff;margin:0 0 20px;font-family:'Space Grotesk',Arial,sans-serif;">
            Cześć __NAME__, jeszcze jeden klik.
          </h1>
          <p class="bfc-body" style="font-size:16px;line-height:1.6;color:#cfd1d4;margin:0 0 28px;">
            Cieszę się, że dosiadasz się do mojego newslettera. Zanim usiądziemy razem na kawę, potrzebuję jednej rzeczy: potwierdź, że to faktycznie Twój adres.
          </p>
          <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 28px;"><tr><td align="center" class="bfc-btn" bgcolor="#ECE183" style="border-radius:12px;background:#ECE183;">
            <a href="__URL__" class="bfc-btn-link" style="display:inline-block;padding:14px 32px;font-size:16px;font-weight:700;color:#1a1b1f;text-decoration:none;font-family:'Space Grotesk',Arial,sans-serif;border-radius:12px;">
              Potwierdzam zapis
            </a>
          </td></tr></table>
          <p class="bfc-muted" style="font-size:13px;line-height:1.6;color:#888;margin:0;">
            Bez tego nie wypijemy razem kawki. Link wygasa za 14 dni. Jeśli to nie Ty się zapisałeś, po prostu zignoruj ten mail.
          </p>
          <p class="bfc-muted" style="font-size:12px;line-height:1.6;color:#a8aab0;margin:18px 0 0;font-family:Arial,sans-serif;">
            Wysłano: __SENT_AT__. To jest nowy link potwierdzający.
          </p>
          <p class="bfc-muted" style="font-size:12px;line-height:1.6;color:#a8aab0;margin:24px 0 0;border-top:1px solid #3a3b3f;padding-top:16px;font-family:Arial,sans-serif;">
            Nie działa przycisk? Skopiuj ten link do przeglądarki:<br>
            <a href="__URL__" class="bfc-brand" style="color:#ECE183 !important;text-decoration:underline;word-break:break-all;">__URL__</a>
          </p>
        </td></tr>
      </table>
      <p class="bfc-faint" style="font-size:12px;color:#666;margin:24px 0 0;font-family:Arial,sans-serif;">
        <a href="https://befreeclub.pl/" class="bfc-brand" style="color:#ECE183;text-decoration:none;">Be Free Club</a> &middot; <a href="https://www.instagram.com/krystianbefree/" class="bfc-brand" style="color:#ECE183;text-decoration:none;">Krystian Rudnik</a>
      </p>
    </td></tr>
  </table>
</body></html>"""


def build_confirm_email_html(name: str, confirm_url: str, sent_at: str) -> str:
    return (
        _CONFIRM_EMAIL_TEMPLATE.replace("__NAME__", escape_html(name))
        .replace("__URL__", escape_html(confirm_url))
        .replace("__SENT_AT__", escape_html(sent_at))
    )
