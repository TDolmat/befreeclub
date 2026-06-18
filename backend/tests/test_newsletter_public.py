"""Testy endpointow /api/newsletter/* (subscribe, confirm, contact, lista admina).

HTTP zewnetrzny (Resend, Sender.net, Meta CAPI) mockowany respx; klient testowy
idzie przez ASGITransport (respx nie przechwytuje jawnego transportu).
DB w /contact i /contact-messages przez override get_session (fake sesja).
"""

import json
import re
import uuid
from datetime import UTC, datetime

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.core import meta_capi
from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.modules.admin.services.rate_limit import _buckets
from app.modules.newsletter.models import ContactMessage
from app.modules.newsletter.routes.public import lead_event_id
from app.modules.newsletter.services import doi

RESEND_URL = "https://api.resend.com/emails"
SENDER_CREATE_URL = "https://api.sender.net/v2/subscribers"
DOI_SECRET = "doi-test-secret"


@pytest.fixture(autouse=True)
def clean_rate_limit_buckets():
    _buckets.clear()
    yield
    _buckets.clear()


@pytest.fixture
def newsletter_configured(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(settings, "NEWSLETTER_DOI_SECRET", DOI_SECRET)
    monkeypatch.setattr(settings, "SENDER_API_TOKEN", "sender-tok")
    monkeypatch.setattr(settings, "NEWSLETTER_FROM_EMAIL", None)
    monkeypatch.setattr(settings, "CONFIRM_URL_BASE", None)
    monkeypatch.setattr(settings, "SENDER_GROUP_IDS", None)
    monkeypatch.setattr(settings, "FRONTEND_URL", None)
    monkeypatch.setattr(settings, "META_PIXEL_ID", None)
    monkeypatch.setattr(settings, "META_CAPI_TOKEN", None)


@pytest.fixture
async def api():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class FakeResult:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows

    def scalar_one(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, results=None):
        self.added = []
        self.commits = 0
        self._results = list(results or [])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def execute(self, stmt):
        return self._results.pop(0)


@pytest.fixture
def fake_db():
    fake = FakeSession()
    app.dependency_overrides[get_session] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_session, None)


# ── POST /api/newsletter/subscribe ──────────────────────────────────────────


@respx.mock
async def test_subscribe_sends_doi_mail(api, newsletter_configured):
    route = respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "msg_1"}))

    resp = await api.post(
        "/api/newsletter/subscribe",
        json={"name": "Jan", "email": "  Jan.Kowalski@X.PL "},
        headers={"x-real-ip": "10.0.0.1"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    body = json.loads(route.calls.last.request.content)
    assert body["to"] == ["jan.kowalski@x.pl"]
    assert body["from"] == "Be Free Club <krystian@befreeclub.pl>"
    assert body["reply_to"] == "krystian@befreeclub.pl"
    # Temat: "{imie} potwierdź swój zapis - nowy link {timestamp pl-PL}".
    assert re.fullmatch(
        r"Jan potwierdź swój zapis - nowy link \d{2}\.\d{2}\.\d{4}, \d{2}:\d{2}:\d{2}",
        body["subject"],
    )
    # Naglowek anty-sklejaniu watkow Gmaila (X-Entity-Ref-ID = uuid proby).
    uuid.UUID(body["headers"]["X-Entity-Ref-ID"])

    # Link potwierdzajacy w mailu niesie wazny token HMAC (email+name+exp 14 dni).
    match = re.search(
        r'href="https://befreeclub\.pl/newsletter/potwierdz\?token=([^"]+)"', body["html"]
    )
    assert match
    payload = doi.verify_token(match.group(1), DOI_SECRET)
    assert payload is not None
    assert payload["email"] == "jan.kowalski@x.pl"
    assert payload["name"] == "Jan"
    expected_exp = doi.now_ms() + doi.DOI_TOKEN_TTL_MS
    assert abs(payload["exp"] - expected_exp) < 60_000


async def test_subscribe_invalid_name(api, newsletter_configured):
    resp = await api.post(
        "/api/newsletter/subscribe",
        json={"name": "  ", "email": "jan@x.pl"},
        headers={"x-real-ip": "10.0.0.2"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "Niepoprawne imię"}

    resp = await api.post(
        "/api/newsletter/subscribe",
        json={"name": "x" * 81, "email": "jan@x.pl"},
        headers={"x-real-ip": "10.0.0.2"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "Niepoprawne imię"}


async def test_subscribe_invalid_email(api, newsletter_configured):
    # Ostatni przypadek: 256 znakow (dokladnie 255 jest legalne, jak w oryginale).
    for bad in ["niepoprawny", "a@b", "a b@c.pl", "a@" + "x" * 251 + ".pl"]:
        resp = await api.post(
            "/api/newsletter/subscribe",
            json={"name": "Jan", "email": bad},
            headers={"x-real-ip": "10.0.0.3"},
        )
        assert resp.status_code == 400
        assert resp.json() == {"error": "Niepoprawny email"}


@respx.mock
async def test_subscribe_resend_failure_returns_502(api, newsletter_configured):
    respx.post(RESEND_URL).mock(return_value=httpx.Response(500, json={"message": "boom"}))

    resp = await api.post(
        "/api/newsletter/subscribe",
        json={"name": "Jan", "email": "jan@x.pl"},
        headers={"x-real-ip": "10.0.0.4"},
    )
    assert resp.status_code == 502
    assert resp.json() == {"error": "Nie udało się wysłać maila potwierdzającego"}


async def test_subscribe_not_configured(api, monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", None)
    monkeypatch.setattr(settings, "NEWSLETTER_DOI_SECRET", None)
    resp = await api.post(
        "/api/newsletter/subscribe",
        json={"name": "Jan", "email": "jan@x.pl"},
        headers={"x-real-ip": "10.0.0.5"},
    )
    assert resp.status_code == 500
    assert resp.json() == {"error": "Internal error"}


async def test_subscribe_rate_limit_per_ip_and_email(api, newsletter_configured):
    # Polityka RL-mail: 5 prob / 15 min -> lock. Kazdy request zuzywa probe.
    for _ in range(5):
        resp = await api.post(
            "/api/newsletter/subscribe",
            json={"name": "", "email": "ofiara@x.pl"},
            headers={"x-real-ip": "10.0.0.6"},
        )
        assert resp.status_code == 400

    resp = await api.post(
        "/api/newsletter/subscribe",
        json={"name": "", "email": "ofiara@x.pl"},
        headers={"x-real-ip": "10.0.0.6"},
    )
    assert resp.status_code == 429
    assert resp.json() == {"error": "Zbyt wiele prób. Spróbuj ponownie później."}

    # Inny email z tego samego IP = osobny bucket (limit per IP+email).
    resp = await api.post(
        "/api/newsletter/subscribe",
        json={"name": "Jan", "email": "zly-email"},
        headers={"x-real-ip": "10.0.0.6"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "Niepoprawny email"}


# ── POST /api/newsletter/confirm ────────────────────────────────────────────


def _valid_token(email: str = "jan@x.pl", name: str = "Jan") -> str:
    exp = doi.now_ms() + doi.DOI_TOKEN_TTL_MS
    return doi.sign_token({"email": email, "name": name, "exp": exp}, DOI_SECRET)


@respx.mock
async def test_confirm_pushes_to_sender(api, newsletter_configured):
    route = respx.post(SENDER_CREATE_URL).mock(return_value=httpx.Response(200, json={}))

    resp = await api.post("/api/newsletter/confirm", json={"token": _valid_token()})

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["name"] == "Jan"
    assert data["eventId"] == lead_event_id("jan@x.pl")

    body = json.loads(route.calls.last.request.content)
    assert body == {
        "email": "jan@x.pl",
        "firstname": "Jan",
        "groups": ["epnLzm", "el06vl"],
        "trigger_automation": True,
    }


@respx.mock
async def test_confirm_sends_capi_lead_when_configured(api, newsletter_configured, monkeypatch):
    monkeypatch.setattr(settings, "META_PIXEL_ID", "12345")
    monkeypatch.setattr(settings, "META_CAPI_TOKEN", "capi-tok")
    respx.post(SENDER_CREATE_URL).mock(return_value=httpx.Response(200, json={}))
    capi_route = respx.post("https://graph.facebook.com/v21.0/12345/events").mock(
        return_value=httpx.Response(200, json={"events_received": 1})
    )

    resp = await api.post(
        "/api/newsletter/confirm",
        json={"token": _valid_token()},
        headers={"x-real-ip": "10.1.0.1", "user-agent": "TestUA/1.0"},
    )
    assert resp.status_code == 200

    payload = json.loads(capi_route.calls.last.request.content)
    event = payload["data"][0]
    assert event["event_name"] == "Lead"
    assert event["event_id"] == lead_event_id("jan@x.pl")
    assert event["action_source"] == "website"
    assert event["event_source_url"] == "https://befreeclub.pl"
    assert event["user_data"]["em"] == [meta_capi.hash_email("jan@x.pl")]
    assert event["user_data"]["client_ip_address"] == "10.1.0.1"
    assert event["user_data"]["client_user_agent"] == "TestUA/1.0"


@respx.mock
async def test_confirm_capi_failure_does_not_break_response(
    api, newsletter_configured, monkeypatch
):
    monkeypatch.setattr(settings, "META_PIXEL_ID", "12345")
    monkeypatch.setattr(settings, "META_CAPI_TOKEN", "capi-tok")
    respx.post(SENDER_CREATE_URL).mock(return_value=httpx.Response(200, json={}))
    respx.post("https://graph.facebook.com/v21.0/12345/events").mock(
        side_effect=httpx.ConnectError("boom")
    )

    resp = await api.post("/api/newsletter/confirm", json={"token": _valid_token()})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_confirm_missing_token(api, newsletter_configured):
    resp = await api.post("/api/newsletter/confirm", json={})
    assert resp.status_code == 400
    assert resp.json() == {"error": "Brak tokenu"}


async def test_confirm_invalid_or_expired_token(api, newsletter_configured):
    resp = await api.post("/api/newsletter/confirm", json={"token": "smieci.smieci"})
    assert resp.status_code == 400
    assert resp.json() == {"error": "Link wygasł lub jest nieprawidłowy"}

    expired = doi.sign_token(
        {"email": "jan@x.pl", "name": "Jan", "exp": doi.now_ms() - 1000}, DOI_SECRET
    )
    resp = await api.post("/api/newsletter/confirm", json={"token": expired})
    assert resp.status_code == 400
    assert resp.json() == {"error": "Link wygasł lub jest nieprawidłowy"}


@respx.mock
async def test_confirm_sender_failure_returns_502(api, newsletter_configured):
    respx.post(SENDER_CREATE_URL).mock(return_value=httpx.Response(422, json={}))
    respx.patch("https://api.sender.net/v2/subscribers/jan%40x.pl").mock(
        return_value=httpx.Response(500, text="boom")
    )

    resp = await api.post("/api/newsletter/confirm", json={"token": _valid_token()})
    assert resp.status_code == 502
    assert resp.json() == {"error": "Nie udało się dokończyć zapisu. Spróbuj za chwilę."}


async def test_confirm_not_configured(api, monkeypatch):
    monkeypatch.setattr(settings, "NEWSLETTER_DOI_SECRET", None)
    monkeypatch.setattr(settings, "SENDER_API_TOKEN", None)
    resp = await api.post("/api/newsletter/confirm", json={"token": "x.y"})
    assert resp.status_code == 500
    assert resp.json() == {"error": "Internal error"}


# ── POST /api/newsletter/contact ────────────────────────────────────────────


@respx.mock
async def test_contact_saves_to_db_and_mails(api, newsletter_configured, fake_db):
    route = respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "msg_2"}))

    resp = await api.post(
        "/api/newsletter/contact",
        json={"name": "Jan", "email": " Jan@X.PL ", "message": "Cześć!\nMam pytanie."},
        headers={"x-real-ip": "10.2.0.1"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # INSERT przez backend (koniec z anon key z przegladarki).
    assert len(fake_db.added) == 1
    row = fake_db.added[0]
    assert isinstance(row, ContactMessage)
    assert row.name == "Jan"
    assert row.email == "jan@x.pl"  # znormalizowany
    assert row.message == "Cześć!\nMam pytanie."
    assert fake_db.commits == 1

    body = json.loads(route.calls.last.request.content)
    assert body["from"] == "Be Free Club <noreply@befreeclub.pl>"
    assert body["to"] == ["krystian@befreeclub.pl"]
    assert body["reply_to"] == "jan@x.pl"
    assert body["subject"] == "Nowa wiadomość od Jan"
    assert "<strong>Imię:</strong> Jan" in body["html"]
    assert "Cześć!<br>Mam pytanie." in body["html"]


@respx.mock
async def test_contact_mail_failure_still_succeeds(api, newsletter_configured, fake_db):
    respx.post(RESEND_URL).mock(return_value=httpx.Response(500, json={"message": "boom"}))

    resp = await api.post(
        "/api/newsletter/contact",
        json={"name": "Jan", "email": "jan@x.pl", "message": "Hej"},
        headers={"x-real-ip": "10.2.0.2"},
    )

    # Mail best-effort: blad logowany, zapis do DB wystarcza.
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(fake_db.added) == 1


@respx.mock
async def test_contact_without_resend_key_silent_success(api, newsletter_configured, fake_db,
                                                         monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", None)
    route = respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={}))

    resp = await api.post(
        "/api/newsletter/contact",
        json={"name": "Jan", "email": "jan@x.pl", "message": "Hej"},
        headers={"x-real-ip": "10.2.0.3"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(fake_db.added) == 1
    assert not route.called  # brak klucza = brak proby wysylki (1:1 cichy sukces)


async def test_contact_invalid_input(api, newsletter_configured, fake_db):
    cases = [
        {"name": "", "email": "jan@x.pl", "message": "Hej"},
        {"name": "x" * 101, "email": "jan@x.pl", "message": "Hej"},
        {"name": "Jan", "email": "zly", "message": "Hej"},
        {"name": "Jan", "email": "jan@x.pl", "message": ""},
        {"name": "Jan", "email": "jan@x.pl", "message": "x" * 5001},
    ]
    for i, body in enumerate(cases):
        resp = await api.post(
            "/api/newsletter/contact", json=body, headers={"x-real-ip": f"10.2.1.{i}"}
        )
        assert resp.status_code == 400
        assert resp.json() == {"error": "Invalid input"}
    assert fake_db.added == []


async def test_contact_rate_limit(api, newsletter_configured, fake_db):
    for _ in range(5):
        resp = await api.post(
            "/api/newsletter/contact",
            json={"name": "", "email": "jan@x.pl", "message": "Hej"},
            headers={"x-real-ip": "10.2.2.1"},
        )
        assert resp.status_code == 400

    resp = await api.post(
        "/api/newsletter/contact",
        json={"name": "Jan", "email": "jan@x.pl", "message": "Hej"},
        headers={"x-real-ip": "10.2.2.1"},
    )
    assert resp.status_code == 429
    assert resp.json() == {"error": "Zbyt wiele prób. Spróbuj ponownie później."}


# ── GET /api/newsletter/contact-messages (admin) ────────────────────────────


async def test_list_contact_messages(api, monkeypatch):
    monkeypatch.setattr(settings, "NODE_ENV", "development")  # dev bypass require_auth

    msg = ContactMessage(name="Jan", email="jan@x.pl", message="Hej")
    msg.id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    msg.created_at = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)

    fake = FakeSession(results=[FakeResult(scalar=7), FakeResult(rows=[msg])])
    app.dependency_overrides[get_session] = lambda: fake
    try:
        resp = await api.get("/api/newsletter/contact-messages?limit=1&offset=2")
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert resp.status_code == 200
    assert resp.json() == {
        "messages": [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "name": "Jan",
                "email": "jan@x.pl",
                "message": "Hej",
                "createdAt": "2026-06-01T10:00:00.000Z",
            }
        ],
        "total": 7,
        "limit": 1,
        "offset": 2,
    }


async def test_list_contact_messages_requires_auth_in_production(api, monkeypatch):
    monkeypatch.setattr(settings, "NODE_ENV", "production")
    resp = await api.get("/api/newsletter/contact-messages")
    assert resp.status_code == 401
    assert resp.json() == {"error": "Unauthorized"}
