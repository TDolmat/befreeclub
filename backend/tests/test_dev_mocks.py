"""Tryb lokalny (app/core/dev_mode.py): auto-mock dev + guard sk_live_.

Konwencja: dev bez klucza = mock (email -> .dev-outbox/, Sender -> log,
Circle members -> fake in-memory), flagi MOCK_* wymuszaja, prod nigdy nie
mockuje (brak klucza = twardy blad jak dotychczas). Stripe nie ma mocka -
klucz sk_live_ poza produkcja blokuje start (SystemExit przy imporcie configu).
"""

import pytest
import respx

from app.core import email
from app.core.config import Settings, guard_dev_stripe_keys, settings
from app.core.dev_mode import resolve_mock
from app.modules.members.services import circle
from app.modules.newsletter.services import sender


@pytest.fixture
def dev_env(monkeypatch):
    monkeypatch.setattr(settings, "NODE_ENV", "development")


# ── resolve_mock (macierz decyzji) ──────────────────────────────────────────


def test_resolve_mock_auto_in_dev(dev_env):
    assert resolve_mock(None, has_key=False) is True  # auto: brak klucza = mock
    assert resolve_mock(None, has_key=True) is False  # auto: klucz = real
    assert resolve_mock(True, has_key=True) is True  # flaga wymusza mock
    assert resolve_mock(False, has_key=False) is False  # flaga wylacza mock


def test_resolve_mock_never_on_production(monkeypatch):
    monkeypatch.setattr(settings, "NODE_ENV", "production")
    assert resolve_mock(None, has_key=False) is False
    assert resolve_mock(True, has_key=True) is False  # flaga ignorowana na prod


# ── Email (Resend) -> .dev-outbox/ ──────────────────────────────────────────


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    path = tmp_path / "outbox"
    monkeypatch.setattr(email, "OUTBOX_DIR", path)
    return path


@respx.mock  # brak route'ow respx: kazdy realny request HTTP by wybuchl
async def test_email_auto_mock_dev_writes_outbox_file(dev_env, outbox, monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", None)
    monkeypatch.setattr(settings, "MOCK_EMAIL", None)  # auto

    assert email.is_mocked() is True
    assert email.is_configured() is True  # guardy route'ow przepuszczaja mock

    msg_id = await email.send_email(
        to="jan@x.pl",
        subject="Potwierdź swój zapis",
        html="<b>hej</b>",
        reply_to="krystian@befreeclub.pl",
        headers={"X-Entity-Ref-ID": "uuid-1"},
    )

    [path] = list(outbox.glob("*.html"))
    assert msg_id == f"mock-{path.name}"
    assert "potwierdz-swoj-zapis" in path.name  # temat zslugifikowany
    content = path.read_text(encoding="utf-8")
    assert f"From: {email.DEFAULT_FROM}" in content
    assert "To: jan@x.pl" in content
    assert "Subject: Potwierdź swój zapis" in content
    assert "Reply-To: krystian@befreeclub.pl" in content
    assert "X-Entity-Ref-ID: uuid-1" in content
    assert content.endswith("<b>hej</b>")


@respx.mock
async def test_email_flag_forces_mock_despite_key(dev_env, outbox, monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_real_key")
    monkeypatch.setattr(settings, "MOCK_EMAIL", True)

    await email.send_email(to="jan@x.pl", subject="x", html="y")

    assert len(list(outbox.glob("*.html"))) == 1  # plik zamiast requestu do Resend


async def test_email_prod_without_key_still_hard_error(monkeypatch):
    monkeypatch.setattr(settings, "NODE_ENV", "production")
    monkeypatch.setattr(settings, "RESEND_API_KEY", None)
    monkeypatch.setattr(settings, "MOCK_EMAIL", True)  # na prod flaga ignorowana

    assert email.is_mocked() is False
    assert email.is_configured() is False
    with pytest.raises(email.EmailConfigError):
        await email.send_email(to="jan@x.pl", subject="x", html="y")


# ── Sender.net ──────────────────────────────────────────────────────────────


@respx.mock
async def test_sender_auto_mock_dev_logs_and_succeeds(dev_env, monkeypatch):
    monkeypatch.setattr(settings, "SENDER_API_TOKEN", None)
    monkeypatch.setattr(settings, "MOCK_SENDER", None)  # auto

    assert sender.is_mocked() is True
    assert sender.is_configured() is True  # confirm route nie zwroci 500
    assert await sender.push_subscriber("jan@x.pl", "Jan") is True  # zero HTTP


# ── Circle (modul members) - fake in-memory ─────────────────────────────────


@respx.mock
async def test_circle_members_mock_never_calls_http(dev_env, monkeypatch):
    monkeypatch.setattr(settings, "CIRCLE_API_TOKEN", None)
    monkeypatch.setattr(settings, "CIRCLE_COMMUNITY_ID", None)
    monkeypatch.setattr(settings, "MOCK_CIRCLE_MEMBERS", None)  # auto
    circle.reset_mock_state()

    assert circle.is_mocked() is True
    assert circle.is_configured() is True  # guard cleanupu przepuszcza mock

    first = await circle.invite("jan@x.pl")
    second = await circle.invite("ola@x.pl", skip_invitation=True)
    assert (first.ok, first.circle_member_id) == (True, "900001")  # rosnace fake id
    assert (second.ok, second.circle_member_id) == (True, "900002")

    assert await circle.remove("900001") is True
    assert await circle.find_member_id_by_email("jan@x.pl") is None
    assert await circle.fetch_all_members() == {}


async def test_circle_members_prod_without_config_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "NODE_ENV", "production")
    monkeypatch.setattr(settings, "CIRCLE_API_TOKEN", None)
    monkeypatch.setattr(settings, "CIRCLE_COMMUNITY_ID", None)
    monkeypatch.setattr(settings, "MOCK_CIRCLE_MEMBERS", True)  # ignorowana na prod

    result = await circle.invite("jan@x.pl")
    assert result.ok is False
    assert result.detail == "Circle credentials not configured"


# ── Stripe guard: sk_live_ poza produkcja blokuje start ─────────────────────


def _settings(**overrides) -> Settings:
    return Settings(CLAUDE_BIN_PATH="claude", **overrides)


def test_guard_blocks_sk_live_in_dev(capsys):
    s = _settings(NODE_ENV="development", STRIPE_SECRET_KEY="sk_live_abc")
    with pytest.raises(SystemExit):
        guard_dev_stripe_keys(s)
    assert "sk_test_" in capsys.readouterr().err  # czytelny komunikat po polsku


def test_guard_blocks_sk_live_legacy_in_dev():
    s = _settings(NODE_ENV="development", STRIPE_LEGACY_SECRET_KEY="sk_live_xyz")
    with pytest.raises(SystemExit):
        guard_dev_stripe_keys(s)


def test_guard_allows_test_keys_in_dev():
    guard_dev_stripe_keys(
        _settings(
            NODE_ENV="development",
            STRIPE_SECRET_KEY="sk_test_abc",
            STRIPE_LEGACY_SECRET_KEY="sk_test_xyz",
        )
    )


def test_guard_ignores_live_keys_on_production():
    guard_dev_stripe_keys(_settings(NODE_ENV="production", STRIPE_SECRET_KEY="sk_live_abc"))
