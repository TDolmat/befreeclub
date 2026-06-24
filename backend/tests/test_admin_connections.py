"""Testy serwisu statusu polaczen (app.modules.admin.services.connections).

Kluczowe gwarancje:
- configured vs unconfigured liczone z env, bez odczytu wartosci,
- test-call: ok / blad HTTP / blad sieci -> status bez wycieku sekretu,
- SEKRET NIGDY nie pojawia sie w odpowiedzi (twardy assert na calym JSON).
"""

import json

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from app.core import secret_box, stripe_client
from app.core.config import settings
from app.modules.admin.services import connections as svc
from app.modules.admin.services import secrets

CIRCLE_MEMBERS_URL = "https://app.circle.so/api/admin/v2/community_members"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
RESEND_DOMAINS_URL = "https://api.resend.com/domains"
STRIPE_BALANCE_URL = "https://api.stripe.com/v1/balance"

# Sekrety, ktorych wartosc NIGDY nie moze trafic do odpowiedzi.
SECRET_VALUES = {
    "sk_test_current_supersecret",
    "sk_test_legacy_supersecret",
    "circle-token-supersecret",
    "openai-key-supersecret",
    "resend-key-supersecret",
    "sender-token-supersecret",
    "meta-token-supersecret",
}


@pytest.fixture(autouse=True)
def _secret_store(monkeypatch):
    """Autouse: czysci cache sekretow i podmienia warstwe DB na in-memory dict,
    zeby _evaluate (secret_status/effective_value) nie dotykal Postgresa. Domyslnie
    pusto = wszystkie integracje schodza na env. Master key Fernet przez monkeypatch
    (set_secret realnie szyfruje). Zwraca dict store_key -> ciphertext."""
    secret_box.reset_cache()
    secrets.invalidate_cache()
    monkeypatch.setattr(settings, "SECRETS_MASTER_KEY", Fernet.generate_key().decode("utf-8"))
    secret_box.reset_cache()

    rows: dict[str, str] = {}

    async def fake_load(key: str):
        return rows.get(key)

    async def fake_upsert(key: str, ciphertext: str, user_id):
        rows[key] = ciphertext

    async def fake_delete(key: str):
        rows.pop(key, None)

    monkeypatch.setattr(secrets, "_db_load", fake_load)
    monkeypatch.setattr(secrets, "_db_upsert", fake_upsert)
    monkeypatch.setattr(secrets, "_db_delete", fake_delete)
    yield rows
    secret_box.reset_cache()
    secrets.invalidate_cache()


@pytest.fixture
def all_secrets(monkeypatch):
    """Ustawia KAZDY sekret na unikalna, rozpoznawalna wartosc."""
    monkeypatch.setattr(settings, "NODE_ENV", "production")  # bez trybu mock
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_current_supersecret")
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", "sk_test_legacy_supersecret")
    monkeypatch.setattr(settings, "CIRCLE_API_TOKEN", "circle-token-supersecret")
    monkeypatch.setattr(settings, "CIRCLE_COMMUNITY_ID", "12345")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "openai-key-supersecret")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "resend-key-supersecret")
    monkeypatch.setattr(settings, "SENDER_API_TOKEN", "sender-token-supersecret")
    monkeypatch.setattr(settings, "META_PIXEL_ID", "963496946601553")
    monkeypatch.setattr(settings, "META_CAPI_TOKEN", "meta-token-supersecret")
    stripe_client.reset_clients()
    yield
    stripe_client.reset_clients()


@pytest.fixture
def no_secrets(monkeypatch):
    """Czysci wszystkie sekrety i wymusza brak trybu mock (jak prod)."""
    monkeypatch.setattr(settings, "NODE_ENV", "production")
    for name in (
        "STRIPE_SECRET_KEY",
        "STRIPE_LEGACY_SECRET_KEY",
        "CIRCLE_API_TOKEN",
        "CIRCLE_COMMUNITY_ID",
        "OPENAI_API_KEY",
        "RESEND_API_KEY",
        "SENDER_API_TOKEN",
        "META_PIXEL_ID",
        "META_CAPI_TOKEN",
    ):
        monkeypatch.setattr(settings, name, None)
    stripe_client.reset_clients()
    yield
    stripe_client.reset_clients()


def _assert_no_secret_leak(payload: object) -> None:
    blob = json.dumps(payload, ensure_ascii=False)
    for secret in SECRET_VALUES:
        assert secret not in blob, f"sekret {secret!r} wyciekl do odpowiedzi"


def _by_key(results) -> dict[str, svc.ConnectionResult]:
    return {r.key: r for r in results}


# ── unconfigured ──────────────────────────────────────────────────────────


async def test_unconfigured_no_secrets(no_secrets):
    results = await svc.list_connections(run_tests=True)
    by = _by_key(results)
    # Wszystkie znane klucze sa obecne.
    assert set(by) == {
        "stripeCurrent",
        "stripeLegacy",
        "circle",
        "openai",
        "resend",
        "sender",
        "metaCapi",
    }
    for r in results:
        assert r.configured is False
        assert r.status == "unconfigured"
        # Brak DB i brak env -> source 'brak', masked None.
        assert r.source == "brak"
        assert r.masked is None
    # Editable flagi: 4 sekrety edytowalne, stripe/circle nie.
    assert by["openai"].editable is True
    assert by["resend"].editable is True
    assert by["sender"].editable is True
    assert by["metaCapi"].editable is True
    assert by["stripeCurrent"].editable is False
    assert by["stripeLegacy"].editable is False
    assert by["circle"].editable is False
    _assert_no_secret_leak([r.to_json() for r in results])


# ── configured: status z is_configured (bez testu) ────────────────────────


async def test_configured_listing_without_tests(all_secrets):
    # run_tests=False: nie odpalamy zadnego HTTP - respx by wybuchl na realnym callu.
    results = await svc.list_connections(run_tests=False)
    by = _by_key(results)
    assert by["stripeCurrent"].configured is True
    assert by["stripeCurrent"].status == "skipped"
    # Stripe status-only z env: source 'env', nieedytowalny, brak maski.
    assert by["stripeCurrent"].source == "env"
    assert by["stripeCurrent"].editable is False
    assert by["stripeCurrent"].masked is None
    assert by["circle"].configured is True
    # Sender/Meta nie maja test-calla -> 'skipped' z detalem nawet przy run_tests.
    assert by["sender"].configured is True
    # Edytowalny klucz z env -> source 'env', maska efektywnej wartosci (nie pelna).
    assert by["openai"].source == "env"
    assert by["openai"].masked == secrets.mask("openai-key-supersecret")
    assert by["openai"].masked is not None
    assert "openai-key-supersecret" != by["openai"].masked
    _assert_no_secret_leak([r.to_json() for r in results])


async def test_no_test_call_apis_skipped(all_secrets):
    results = await svc.list_connections(run_tests=True)
    by = _by_key(results)
    # Sender i Meta CAPI: brak taniego/bezpiecznego testu -> 'skipped'.
    assert by["sender"].status == "skipped"
    assert by["sender"].detail == "brak test-call"
    assert by["metaCapi"].status == "skipped"


# ── test-call: ok ─────────────────────────────────────────────────────────


@respx.mock
async def test_circle_test_ok(all_secrets):
    route = respx.get(url__startswith=CIRCLE_MEMBERS_URL).mock(
        return_value=httpx.Response(200, json={"records": []})
    )
    result = await svc.test_connection("circle")
    assert result is not None
    assert result.status == "ok"
    assert route.called
    # Token poszedl w naglowku Authorization, nie w odpowiedzi.
    assert "Authorization" in route.calls.last.request.headers
    _assert_no_secret_leak(result.to_json())


@respx.mock
async def test_openai_test_ok(all_secrets):
    respx.get(OPENAI_MODELS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    result = await svc.test_connection("openai")
    assert result is not None
    assert result.status == "ok"
    _assert_no_secret_leak(result.to_json())


@respx.mock
async def test_resend_test_ok(all_secrets):
    respx.get(RESEND_DOMAINS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    result = await svc.test_connection("resend")
    assert result is not None
    assert result.status == "ok"
    _assert_no_secret_leak(result.to_json())


@respx.mock
async def test_stripe_current_test_ok(all_secrets):
    respx.get(STRIPE_BALANCE_URL).mock(
        return_value=httpx.Response(200, json={"object": "balance", "available": []})
    )
    result = await svc.test_connection("stripeCurrent")
    assert result is not None
    assert result.status == "ok"
    _assert_no_secret_leak(result.to_json())


# ── test-call Stripe: zly klucz (401) -> error bez wycieku fragmentu klucza ─


@respx.mock
async def test_stripe_bad_key_no_secret_and_recognizable_status(all_secrets):
    # Stripe przy zlym kluczu echo'uje fragment klucza w komunikacie API
    # (prefiks + 4 ostatnie znaki). SDK zamieni to 401 w AuthenticationError,
    # ktorego str() ZAWIERA ten fragment. Detail NIE moze go przepuscic.
    respx.get(STRIPE_BALANCE_URL).mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {
                    "message": "Invalid API Key provided: sk_test_current_supersecret",
                    "type": "invalid_request_error",
                }
            },
        )
    )
    result = await svc.test_connection("stripeCurrent")
    assert result is not None
    assert result.status == "error"
    # (a) zaden fragment klucza ani prefiks 'sk_' nie trafia do detalu.
    assert "sk_" not in result.detail
    assert "supersecret" not in result.detail
    # (b) AuthenticationError mapuje sie na rozpoznawalny status (matcher 'http 401').
    assert result.detail == "HTTP 401"
    _assert_no_secret_leak(result.to_json())


# ── test-call: blad HTTP (np. 401) -> error bez wycieku ───────────────────


@respx.mock
async def test_circle_test_http_error(all_secrets):
    respx.get(url__startswith=CIRCLE_MEMBERS_URL).mock(
        return_value=httpx.Response(401, text="Invalid token circle-token-supersecret")
    )
    result = await svc.test_connection("circle")
    assert result is not None
    assert result.status == "error"
    assert "401" in result.detail
    # Body dostawcy (z echo sekretu) NIE moze trafic do detalu.
    _assert_no_secret_leak(result.to_json())


@respx.mock
async def test_openai_test_http_error(all_secrets):
    respx.get(OPENAI_MODELS_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    result = await svc.test_connection("openai")
    assert result is not None
    assert result.status == "error"
    assert "401" in result.detail
    _assert_no_secret_leak(result.to_json())


# ── test-call: blad sieci -> error bez wycieku ────────────────────────────


@respx.mock
async def test_circle_test_network_error(all_secrets):
    respx.get(url__startswith=CIRCLE_MEMBERS_URL).mock(side_effect=httpx.ConnectError("boom"))
    result = await svc.test_connection("circle")
    assert result is not None
    assert result.status == "error"
    _assert_no_secret_leak(result.to_json())


@respx.mock
async def test_openai_test_network_error(all_secrets):
    respx.get(OPENAI_MODELS_URL).mock(side_effect=httpx.ConnectError("boom"))
    result = await svc.test_connection("openai")
    assert result is not None
    assert result.status == "error"
    _assert_no_secret_leak(result.to_json())


# ── tryb dev/mock: brak klucza -> 'mock', nie 'error'/500 ─────────────────


async def test_dev_mock_status(monkeypatch):
    monkeypatch.setattr(settings, "NODE_ENV", "development")
    # Brak kluczy serwisow majacych mock (circle members, resend, sender).
    monkeypatch.setattr(settings, "RESEND_API_KEY", None)
    monkeypatch.setattr(settings, "MOCK_EMAIL", None)  # auto-mock w dev
    monkeypatch.setattr(settings, "SENDER_API_TOKEN", None)
    monkeypatch.setattr(settings, "MOCK_SENDER", None)
    monkeypatch.setattr(settings, "CIRCLE_API_TOKEN", None)
    monkeypatch.setattr(settings, "CIRCLE_COMMUNITY_ID", None)
    monkeypatch.setattr(settings, "MOCK_CIRCLE_MEMBERS", None)

    results = await svc.list_connections(run_tests=True)
    by = _by_key(results)
    assert by["resend"].status == "mock"
    assert by["sender"].status == "mock"
    assert by["circle"].status == "mock"


# ── nieznany klucz ────────────────────────────────────────────────────────


async def test_unknown_key_returns_none(all_secrets):
    assert await svc.test_connection("nopeNotAKey") is None


# ── edytowalne sekrety: set / clear / reveal ──────────────────────────────


async def test_set_secret_makes_openai_panel_configured(no_secrets):
    # Env puste, baza pusta -> openai unconfigured, source 'brak'.
    before = _by_key(await svc.list_connections(run_tests=False))["openai"]
    assert before.configured is False
    assert before.source == "brak"

    await svc.set_secret("openai", "openai-panel-secret", user_id=7)

    after = _by_key(await svc.list_connections(run_tests=False))["openai"]
    # Mimo pustego env: source 'panel', configured True, maska efektywnej wartosci.
    assert after.source == "panel"
    assert after.configured is True
    assert after.masked == secrets.mask("openai-panel-secret")
    # Pelna wartosc NIGDY w statusie.
    blob = json.dumps(after.to_json(), ensure_ascii=False)
    assert "openai-panel-secret" not in blob


async def test_set_then_clear_falls_back_to_env(all_secrets):
    # all_secrets ma env openai -> najpierw source 'env'.
    assert _by_key(await svc.list_connections(run_tests=False))["openai"].source == "env"
    await svc.set_secret("openai", "panel-override", user_id=None)
    assert _by_key(await svc.list_connections(run_tests=False))["openai"].source == "panel"
    await svc.clear_secret("openai", user_id=None)
    # Po clear wraca na env.
    after = _by_key(await svc.list_connections(run_tests=False))["openai"]
    assert after.source == "env"


async def test_reveal_returns_full_value(no_secrets):
    await svc.set_secret("resend", "re_full_value_123456", user_id=1)
    revealed = await svc.reveal_secret("resend")
    assert revealed == "re_full_value_123456"


async def test_reveal_env_value_when_no_db(all_secrets):
    # Brak wiersza, env ma openai -> reveal pokazuje wartosc z env.
    assert await svc.reveal_secret("openai") == "openai-key-supersecret"


async def test_reveal_none_when_brak(no_secrets):
    assert await svc.reveal_secret("openai") is None


@pytest.mark.parametrize("key", ["stripeCurrent", "stripeLegacy", "circle"])
async def test_set_clear_reveal_not_editable_raises(no_secrets, key):
    with pytest.raises(svc.ConnectionNotEditable):
        await svc.set_secret(key, "whatever", user_id=1)
    with pytest.raises(svc.ConnectionNotEditable):
        await svc.clear_secret(key, user_id=1)
    with pytest.raises(svc.ConnectionNotEditable):
        await svc.reveal_secret(key)


async def test_set_unknown_key_raises(no_secrets):
    with pytest.raises(svc.ConnectionNotEditable):
        await svc.set_secret("nopeNotAKey", "x", user_id=1)


# ── resolver DB > env w kliencie/tescie integracji ────────────────────────


@respx.mock
async def test_openai_test_uses_db_key_over_env(all_secrets):
    # Env ma 'openai-key-supersecret', panel nadpisuje wlasnym kluczem. Test-call
    # OpenAI musi uzyc klucza z DB (panel), nie z env.
    await svc.set_secret("openai", "db-openai-key", user_id=1)
    route = respx.get(OPENAI_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    result = await svc.test_connection("openai")
    assert result is not None
    assert result.status == "ok"
    # Naglowek Authorization niosl klucz z DB, nie z env.
    sent = route.calls.last.request.headers["Authorization"]
    assert sent == "Bearer db-openai-key"
    _assert_no_secret_leak(result.to_json())
