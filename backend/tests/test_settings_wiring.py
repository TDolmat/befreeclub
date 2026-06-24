"""Wiring knobow tunable: edycja w panelu (admin.settings) FAKTYCZNIE zmienia
zachowanie konsumenta po max ~30 s cache TTL, bez restartu.

Dla kazdego przepietego knoba sprawdzamy DWIE rzeczy:
1. brak wiersza w DB (zimny cache) = env fallback (zachowanie 1:1),
2. set_setting(...) -> efektywna wartosc czytana przez konsumenta sie zmienia.

DB nie jest tu potrzebna: nadpisanie z panelu symulujemy podgrzewajac procesowy
cache serwisu ustawien (store._cache) tym, co realnie zapisalby set_setting -
{"value": ...} pod kluczem. To ta sama sciezka odczytu (cache) co async
get_setting i sync get_effective_sync, wiec test pokrywa oba typy akcesorow.
"""

import time

import pytest

from app.core import meta_capi
from app.core.config import settings
from app.modules.admin.services import settings as store
from app.modules.billing.services import ebook
from app.modules.members.services import circle
from app.modules.newsletter.services import sender


@pytest.fixture(autouse=True)
def _clear_cache():
    store.invalidate_cache()
    yield
    store.invalidate_cache()


def _seed_db_override(store_key: str, value) -> None:
    """Symuluje wiersz admin.settings[store_key] = {"value": value} w cieplym
    cache - dokladnie to, co zostawia set_setting po zapisie + invalidacji."""
    store._cache[store_key] = ({"value": value}, time.time() * 1000)


# ── SENDER_GROUP_IDS (sync: sender.group_ids) ────────────────────────────────


def test_sender_group_ids_env_fallback_when_no_db(monkeypatch):
    monkeypatch.setattr(settings, "SENDER_GROUP_IDS", "envA,envB")
    assert sender.group_ids() == ["envA", "envB"]


def test_sender_group_ids_db_overrides_env(monkeypatch):
    monkeypatch.setattr(settings, "SENDER_GROUP_IDS", "envA,envB")
    _seed_db_override("newsletter.sender_group_ids", "dbX, dbY ")
    assert sender.group_ids() == ["dbX", "dbY"]


def test_sender_group_ids_default_when_no_db_no_env(monkeypatch):
    # Brak env i brak DB = wbudowany default CSV (1:1 z dotychczasowym).
    monkeypatch.setattr(settings, "SENDER_GROUP_IDS", None)
    assert sender.group_ids() == ["epnLzm", "el06vl"]


# ── EBOOK_FILE_PATH (async: ebook._consume reads await effective) ────────────
# i FRONTEND_URL (sync: ebook.download_url_for) ──────────────────────────────


def test_ebook_download_url_env_fallback_when_no_db(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://env.example")
    assert ebook.download_url_for("tok") == "https://env.example/ebook/pobierz?token=tok"


def test_ebook_download_url_db_overrides_env(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://env.example")
    _seed_db_override("billing.frontend_url", "https://db.example")
    assert ebook.download_url_for("tok") == "https://db.example/ebook/pobierz?token=tok"


async def test_ebook_file_path_env_fallback_when_no_db(monkeypatch):
    from app.modules.admin.services import settings_catalog

    monkeypatch.setattr(settings, "EBOOK_FILE_PATH", "/env/ebook.pdf")
    assert await settings_catalog.effective("ebookFilePath") == "/env/ebook.pdf"


async def test_ebook_file_path_db_overrides_env(monkeypatch):
    from app.modules.admin.services import settings_catalog

    monkeypatch.setattr(settings, "EBOOK_FILE_PATH", "/env/ebook.pdf")
    _seed_db_override("billing.ebook_file_path", "/db/ebook.pdf")
    assert await settings_catalog.effective("ebookFilePath") == "/db/ebook.pdf"


# ── CIRCLE_COMMUNITY_ID (sync: circle._community_id / _credentials) ──────────


def test_circle_community_id_env_fallback_when_no_db(monkeypatch):
    monkeypatch.setattr(settings, "CIRCLE_COMMUNITY_ID", "111")
    assert circle._community_id() == "111"


def test_circle_community_id_db_overrides_env(monkeypatch):
    monkeypatch.setattr(settings, "CIRCLE_COMMUNITY_ID", "111")
    _seed_db_override("members.circle_community_id", "222")
    assert circle._community_id() == "222"


def test_circle_credentials_use_effective_community_id(monkeypatch):
    monkeypatch.setattr(settings, "CIRCLE_API_TOKEN", "tok")
    monkeypatch.setattr(settings, "CIRCLE_COMMUNITY_ID", "111")
    _seed_db_override("members.circle_community_id", "999")
    token, community_id = circle._credentials()
    assert (token, community_id) == ("tok", "999")


# ── META_PIXEL_ID (sync: meta_capi._pixel_id / is_configured) ────────────────


def test_meta_pixel_id_env_fallback_when_no_db(monkeypatch):
    monkeypatch.setattr(settings, "META_PIXEL_ID", "env-pixel")
    assert meta_capi._pixel_id() == "env-pixel"


def test_meta_pixel_id_db_overrides_env(monkeypatch):
    monkeypatch.setattr(settings, "META_PIXEL_ID", "env-pixel")
    _seed_db_override("analytics.meta_pixel_id", "db-pixel")
    assert meta_capi._pixel_id() == "db-pixel"


def test_meta_is_configured_follows_effective_pixel(monkeypatch):
    monkeypatch.setattr(settings, "META_CAPI_TOKEN", "tok")
    monkeypatch.setattr(settings, "META_PIXEL_ID", None)
    # Brak env + brak DB = wylaczone.
    assert meta_capi.is_configured() is False
    # Panel ustawia Pixel ID -> wlaczone (token sekretny dalej z env).
    _seed_db_override("analytics.meta_pixel_id", "db-pixel")
    assert meta_capi.is_configured() is True


# ── Email/URL knoby async (effective) - cancellation/newsletter from email ───


async def test_frontend_url_async_env_fallback_and_db_override(monkeypatch):
    from app.modules.admin.services import settings_catalog

    monkeypatch.setattr(settings, "FRONTEND_URL", "https://env.pl")
    assert await settings_catalog.effective("frontendUrl") == "https://env.pl"
    _seed_db_override("billing.frontend_url", "https://db.pl")
    assert await settings_catalog.effective("frontendUrl") == "https://db.pl"


async def test_cancellation_from_email_async_env_fallback_and_db_override(monkeypatch):
    from app.modules.admin.services import settings_catalog

    monkeypatch.setattr(settings, "CANCELLATION_FROM_EMAIL", "Env <env@x.pl>")
    assert await settings_catalog.effective("cancellationFromEmail") == "Env <env@x.pl>"
    _seed_db_override("billing.cancellation_from_email", "Db <db@x.pl>")
    assert await settings_catalog.effective("cancellationFromEmail") == "Db <db@x.pl>"


async def test_newsletter_from_email_async_env_fallback_and_db_override(monkeypatch):
    from app.modules.admin.services import settings_catalog

    monkeypatch.setattr(settings, "NEWSLETTER_FROM_EMAIL", "Env <env@x.pl>")
    assert await settings_catalog.effective("newsletterFromEmail") == "Env <env@x.pl>"
    _seed_db_override("newsletter.from_email", "Db <db@x.pl>")
    assert await settings_catalog.effective("newsletterFromEmail") == "Db <db@x.pl>"


async def test_confirm_url_base_async_env_fallback_and_db_override(monkeypatch):
    from app.modules.admin.services import settings_catalog

    monkeypatch.setattr(settings, "CONFIRM_URL_BASE", "https://env.pl/c")
    assert await settings_catalog.effective("confirmUrlBase") == "https://env.pl/c"
    _seed_db_override("newsletter.confirm_url_base", "https://db.pl/c")
    assert await settings_catalog.effective("confirmUrlBase") == "https://db.pl/c"


# ── get_effective_sync: kontrakt zimnego cache ───────────────────────────────


def test_get_effective_sync_cold_cache_returns_env():
    # Zimny cache (brak wpisu) NIGDY nie blokuje na DB - zwraca env fallback.
    store.invalidate_cache()
    assert store.get_effective_sync("x.cold", env_fallback="env-val") == "env-val"


def test_get_effective_sync_stale_cache_falls_back_to_env():
    # Wpis starszy niz TTL = traktowany jak zimny (env fallback), nie DB.
    store._cache["x.stale"] = ({"value": "db-val"}, time.time() * 1000 - store.CACHE_MS - 1)
    assert store.get_effective_sync("x.stale", env_fallback="env-val") == "env-val"


def test_get_effective_sync_warm_cache_returns_db():
    store._cache["x.warm"] = ({"value": "db-val"}, time.time() * 1000)
    assert store.get_effective_sync("x.warm", env_fallback="env-val") == "db-val"


def test_get_effective_sync_warm_cache_without_value_returns_env():
    # Wiersz bez pola "value" (np. null przywraca env) = env fallback.
    store._cache["x.novalue"] = ({"enabled": False}, time.time() * 1000)
    assert store.get_effective_sync("x.novalue", env_fallback="env-val") == "env-val"
