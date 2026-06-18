"""Testy tokenu DOI newslettera (format 1:1 z edge functions) + szablonu maila."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

from app.modules.newsletter.services import doi

SECRET = "doi-test-secret"


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _future_exp() -> int:
    return doi.now_ms() + 60_000


def test_sign_token_matches_js_format():
    """JSON bez spacji, klucze email/name/exp, b64url bez paddingu,
    podpis HMAC po STRINGU b64url payloadu - dokladnie jak signToken w Deno."""
    payload = {"email": "jan@x.pl", "name": "Żaneta", "exp": 1750000000000}
    token = doi.sign_token(payload, SECRET)

    assert "=" not in token
    data_b64, sig_b64 = token.split(".")
    assert (
        _b64url_decode(data_b64).decode("utf-8")
        == '{"email":"jan@x.pl","name":"Żaneta","exp":1750000000000}'
    )
    expected_sig = hmac.new(SECRET.encode(), data_b64.encode(), hashlib.sha256).digest()
    assert _b64url_decode(sig_b64) == expected_sig


def test_verify_roundtrip():
    exp = _future_exp()
    token = doi.sign_token({"email": "jan@x.pl", "name": "Jan", "exp": exp}, SECRET)
    payload = doi.verify_token(token, SECRET)
    assert payload == {"email": "jan@x.pl", "name": "Jan", "exp": exp}


def test_verify_expired_token():
    token = doi.sign_token(
        {"email": "jan@x.pl", "name": "Jan", "exp": doi.now_ms() - 1000}, SECRET
    )
    assert doi.verify_token(token, SECRET) is None


def test_verify_tampered_payload_keeps_signature():
    token = doi.sign_token({"email": "jan@x.pl", "name": "Jan", "exp": _future_exp()}, SECRET)
    _, sig_b64 = token.split(".")
    forged_payload = json.dumps(
        {"email": "haker@x.pl", "name": "Jan", "exp": _future_exp()}, separators=(",", ":")
    )
    forged_b64 = base64.urlsafe_b64encode(forged_payload.encode()).decode().rstrip("=")
    assert doi.verify_token(f"{forged_b64}.{sig_b64}", SECRET) is None


def test_verify_tampered_signature():
    token = doi.sign_token({"email": "jan@x.pl", "name": "Jan", "exp": _future_exp()}, SECRET)
    data_b64, sig_b64 = token.split(".")
    flipped = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
    assert doi.verify_token(f"{data_b64}.{flipped}", SECRET) is None


def test_verify_wrong_secret():
    token = doi.sign_token({"email": "jan@x.pl", "name": "Jan", "exp": _future_exp()}, SECRET)
    assert doi.verify_token(token, "inny-sekret") is None


def test_verify_malformed_tokens():
    assert doi.verify_token("", SECRET) is None
    assert doi.verify_token("bez-kropki", SECRET) is None
    assert doi.verify_token("a.b.c", SECRET) is None
    assert doi.verify_token("!!!.???", SECRET) is None


def test_verify_missing_fields():
    # JS: !payload.email || !payload.exp -> null.
    no_email = doi.sign_token({"name": "Jan", "exp": _future_exp()}, SECRET)
    assert doi.verify_token(no_email, SECRET) is None
    no_exp = doi.sign_token({"email": "jan@x.pl", "name": "Jan"}, SECRET)
    assert doi.verify_token(no_exp, SECRET) is None
    zero_exp = doi.sign_token({"email": "jan@x.pl", "name": "Jan", "exp": 0}, SECRET)
    assert doi.verify_token(zero_exp, SECRET) is None


def test_verify_uses_constant_time_compare(monkeypatch):
    calls: list[tuple[bytes, bytes]] = []
    real_compare = hmac.compare_digest

    def spy(a, b):
        calls.append((bytes(a), bytes(b)))
        return real_compare(a, b)

    monkeypatch.setattr("app.modules.newsletter.services.doi.hmac.compare_digest", spy)
    token = doi.sign_token({"email": "jan@x.pl", "name": "Jan", "exp": _future_exp()}, SECRET)
    assert doi.verify_token(token, SECRET) is not None
    assert len(calls) == 1
    given, expected = calls[0]
    assert given == expected  # poprawny token: podpisy identyczne


def test_sent_at_label_warsaw_format():
    # 10.06.2026 12:00 UTC = 14:00 w Warszawie (CEST) - format pl-PL short/medium.
    label = doi.sent_at_label(datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC))
    assert label == "10.06.2026, 14:00:00"


def test_sent_at_label_single_digit_day_no_padding():
    """REGRESJA (review 2.1): CLDR pl short = d.MM.y - dzien BEZ zera
    wiodacego (Intl daje "3.06.2026", nie "03.06.2026"); godzina padowana."""
    label = doi.sent_at_label(datetime(2026, 6, 3, 6, 5, 9, tzinfo=UTC))
    assert label == "3.06.2026, 08:05:09"


def test_escape_html_no_apostrophe():
    # escapeHtml z newsletter-subscribe NIE escapuje apostrofu (1:1).
    assert doi.escape_html('<b>"A&B"</b> \'x\'') == "&lt;b&gt;&quot;A&amp;B&quot;&lt;/b&gt; 'x'"


def test_build_confirm_email_html():
    html = doi.build_confirm_email_html(
        "Jan <Kowalski>", "https://befreeclub.pl/newsletter/potwierdz?token=abc.def",
        "10.06.2026, 14:00:00",
    )
    assert "Cześć Jan &lt;Kowalski&gt;, jeszcze jeden klik." in html
    assert html.count('href="https://befreeclub.pl/newsletter/potwierdz?token=abc.def"') == 2
    assert "Wysłano: 10.06.2026, 14:00:00. To jest nowy link potwierdzający." in html
    assert "Potwierdzam zapis" in html
    assert "Link wygasa za 14 dni" in html
    assert html.startswith("<!DOCTYPE html>")
