"""Testy store edytowalnych sekretow (app/modules/admin/services/secrets.py).

Bez Postgresa - warstwa DB (_db_load/_db_upsert/_db_delete) podmieniona na
in-memory dict (wzorzec test_admin_settings_api.fake_store). Master key Fernet
ustawiany przez monkeypatch. Weryfikujemy: round-trip, ze w 'bazie' siedzi
ciphertext (nie plaintext), cache sync, clear, precedencje DB>env, status bez
ujawniania wartosci, maske, effective_value oraz bezpieczny fallback bez klucza.
"""

import pytest
from cryptography.fernet import Fernet

from app.core import secret_box
from app.core.config import settings as env
from app.modules.admin.services import secrets


@pytest.fixture(autouse=True)
def _clean():
    secret_box.reset_cache()
    secrets.invalidate_cache()
    yield
    secret_box.reset_cache()
    secrets.invalidate_cache()


@pytest.fixture
def master_key(monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(env, "SECRETS_MASTER_KEY", key)
    secret_box.reset_cache()
    return key


@pytest.fixture
def fake_db(monkeypatch):
    """In-memory podmiana warstwy DB. Zwraca dict store_key -> ciphertext."""
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
    return rows


# ── round-trip i przechowywanie ──────────────────────────────────────────────


async def test_set_then_get_round_trip(master_key, fake_db):
    await secrets.set_secret("openai.api_key", "sk-tajne", 7)
    secrets.invalidate_cache()  # wymus odczyt z 'bazy', nie z cache zapisu
    assert await secrets.get_secret("openai.api_key") == "sk-tajne"


async def test_db_holds_ciphertext_not_plaintext(master_key, fake_db):
    await secrets.set_secret("resend.api_key", "re_jawny_klucz", 1)
    stored = fake_db["resend.api_key"]
    assert stored != "re_jawny_klucz"
    assert "re_jawny_klucz" not in stored
    # I daje sie odszyfrowac z powrotem niezaleznie od cache.
    assert secret_box.decrypt(stored) == "re_jawny_klucz"


async def test_get_secret_sync_uses_cache(master_key, fake_db):
    # Zimny cache -> sync zwraca None (nie dotyka DB).
    assert secrets.get_secret_sync("sender.api_token") is None
    await secrets.set_secret("sender.api_token", "tok-123", None)
    # set ogrzal cache -> sync widzi wartosc.
    assert secrets.get_secret_sync("sender.api_token") == "tok-123"


async def test_clear_removes_and_falls_back_to_env(master_key, fake_db, monkeypatch):
    monkeypatch.setattr(env, "OPENAI_API_KEY", "env-openai")
    await secrets.set_secret("openai.api_key", "panel-openai", None)
    assert await secrets.resolve("openai.api_key") == "panel-openai"
    await secrets.clear_secret("openai.api_key", None)
    assert await secrets.get_secret("openai.api_key") is None
    assert await secrets.resolve("openai.api_key") == "env-openai"


# ── precedencja DB > env ─────────────────────────────────────────────────────


async def test_resolve_db_beats_env(master_key, fake_db, monkeypatch):
    monkeypatch.setattr(env, "META_CAPI_TOKEN", "env-meta")
    await secrets.set_secret("meta.capi_token", "panel-meta", None)
    assert await secrets.resolve("meta.capi_token") == "panel-meta"


async def test_resolve_falls_back_to_env_when_no_row(master_key, fake_db, monkeypatch):
    monkeypatch.setattr(env, "RESEND_API_KEY", "env-resend")
    assert await secrets.resolve("resend.api_key") == "env-resend"


async def test_resolve_sync_db_beats_env(master_key, fake_db, monkeypatch):
    monkeypatch.setattr(env, "SENDER_API_TOKEN", "env-sender")
    await secrets.set_secret("sender.api_token", "panel-sender", None)
    assert secrets.resolve_sync("sender.api_token") == "panel-sender"


async def test_resolve_sync_cold_cache_uses_env(master_key, fake_db, monkeypatch):
    monkeypatch.setattr(env, "SENDER_API_TOKEN", "env-sender")
    # Wiersz jest w 'bazie', ale cache zimny -> sync nie dotyka DB, schodzi na env.
    await secrets.set_secret("sender.api_token", "panel-sender", None)
    secrets.invalidate_cache()
    assert secrets.resolve_sync("sender.api_token") == "env-sender"


# ── status (bez ujawniania wartosci) ─────────────────────────────────────────


async def test_status_panel(master_key, fake_db):
    await secrets.set_secret("openai.api_key", "x", None)
    assert await secrets.secret_status("openai.api_key") == "panel"


async def test_status_env(master_key, fake_db, monkeypatch):
    monkeypatch.setattr(env, "OPENAI_API_KEY", "env-x")
    assert await secrets.secret_status("openai.api_key") == "env"


async def test_status_brak(master_key, fake_db, monkeypatch):
    monkeypatch.setattr(env, "OPENAI_API_KEY", None)
    assert await secrets.secret_status("openai.api_key") == "brak"


async def test_status_panel_even_when_undecryptable(fake_db, monkeypatch):
    # Wiersz jest, ale master key niedostepny - status nadal 'panel' (obecnosc
    # wiersza, nie wartosc).
    monkeypatch.setattr(env, "SECRETS_MASTER_KEY", None)
    secret_box.reset_cache()
    fake_db["openai.api_key"] = "jakis-ciphertext"
    assert await secrets.secret_status("openai.api_key") == "panel"


# ── mask ─────────────────────────────────────────────────────────────────────


def test_mask_none():
    assert secrets.mask(None) is None


def test_mask_short():
    assert secrets.mask("abcd1234") == "••••"


def test_mask_long():
    assert secrets.mask("sk-1234567890abcd") == "sk-1…abcd"


# ── effective_value (reveal) ─────────────────────────────────────────────────


async def test_effective_value_db(master_key, fake_db):
    await secrets.set_secret("openai.api_key", "panel-val", None)
    assert await secrets.effective_value("openai.api_key") == "panel-val"


async def test_effective_value_env(master_key, fake_db, monkeypatch):
    monkeypatch.setattr(env, "OPENAI_API_KEY", "env-val")
    assert await secrets.effective_value("openai.api_key") == "env-val"


# ── bezpieczny fallback bez master key ───────────────────────────────────────


async def test_no_master_key_get_secret_none_resolve_env(fake_db, monkeypatch):
    # Bez master key: ustawienie sie nie uda (set rzuca), wiec wstrzykujemy obcy
    # ciphertext bezposrednio. get_secret -> None (nie crash), resolve -> env.
    monkeypatch.setattr(env, "SECRETS_MASTER_KEY", None)
    monkeypatch.setattr(env, "RESEND_API_KEY", "env-resend")
    secret_box.reset_cache()
    fake_db["resend.api_key"] = "nieodszyfrowywalny"
    assert await secrets.get_secret("resend.api_key") is None
    assert await secrets.resolve("resend.api_key") == "env-resend"


async def test_no_master_key_set_secret_raises(fake_db, monkeypatch):
    monkeypatch.setattr(env, "SECRETS_MASTER_KEY", None)
    secret_box.reset_cache()
    with pytest.raises(secret_box.SecretBoxUnavailable):
        await secrets.set_secret("openai.api_key", "x", None)


# ── nieznany klucz ───────────────────────────────────────────────────────────


async def test_unknown_key_get_none(master_key, fake_db):
    assert await secrets.get_secret("stripe.secret") is None


async def test_unknown_key_set_raises_keyerror(master_key, fake_db):
    with pytest.raises(KeyError):
        await secrets.set_secret("stripe.secret", "x", None)


async def test_unknown_key_clear_raises_keyerror(master_key, fake_db):
    with pytest.raises(KeyError):
        await secrets.clear_secret("circle.token", None)


# ── warm_cache + last-known-good (sync nie wygasa do None) ────────────────────


async def test_warm_cache_makes_sync_see_panel_key(master_key, fake_db):
    await secrets.set_secret("openai.api_key", "panel-openai", None)
    secrets.invalidate_cache()  # zimny cache jak po restarcie procesu
    assert secrets.get_secret_sync("openai.api_key") is None  # przed ogrzaniem
    await secrets.warm_cache()
    assert secrets.get_secret_sync("openai.api_key") == "panel-openai"


async def test_sync_last_known_good_survives_ttl(master_key, fake_db, monkeypatch):
    # Po wygasnieciu TTL sync nadal oddaje ostatnia znana wartosc (nie None),
    # zeby worker nie oscylowal w trybie panel-only-bez-env.
    monkeypatch.setattr(env, "OPENAI_API_KEY", None)
    await secrets.set_secret("openai.api_key", "panel-openai", None)
    val, _ = secrets._cache["openai.api_key"]
    secrets._cache["openai.api_key"] = (val, 0.0)  # postarz wpis ponad TTL
    assert secrets.get_secret_sync("openai.api_key") == "panel-openai"
    assert secrets.resolve_sync("openai.api_key") == "panel-openai"


# ── odpornosc na blad DB (ekran Polaczen nie wywala 500) ─────────────────────


async def test_db_failure_get_secret_returns_none(master_key, fake_db, monkeypatch):
    async def boom(key: str):
        raise RuntimeError("db down")

    monkeypatch.setattr(secrets, "_db_load", boom)
    assert await secrets.get_secret("openai.api_key") is None  # nie crash


async def test_db_failure_status_degrades_safely(master_key, fake_db, monkeypatch):
    async def boom(key: str):
        raise RuntimeError("db down")

    monkeypatch.setattr(secrets, "_db_load", boom)
    monkeypatch.setattr(env, "OPENAI_API_KEY", "env-x")
    assert await secrets.secret_status("openai.api_key") == "env"  # nie crash
    monkeypatch.setattr(env, "OPENAI_API_KEY", None)
    assert await secrets.secret_status("openai.api_key") == "brak"


# ── mask: prog 12 znakow ─────────────────────────────────────────────────────


def test_mask_medium_short_fully_hidden():
    # 10 znakow < 12 -> pelna maska (wczesniej odslanialoby 8 z 10).
    assert secrets.mask("0123456789") == "••••"


def test_mask_boundary_12_shows_ends():
    assert secrets.mask("0123456789ab") == "0123…89ab"
