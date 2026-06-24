"""[fundament ustawien]: czysta logika centralnego serwisu admin.settings.

Bez DB - testujemy regule bezpieczenstwa (brak wiersza = bezpieczny default,
NIGDY enabled=true) oraz precedencje get_effective (DB > env > safe_default).
Odczyt z bazy (get_setting._load_fresh) mockujemy, zeby suite nie zalezal od
Postgresa - integracja DB jest weryfikowana migracja + seedami osobno.
"""

import pytest

from app.modules.admin.services import settings as svc


@pytest.fixture(autouse=True)
def _clear_cache():
    svc.invalidate_cache()
    yield
    svc.invalidate_cache()


def test_safe_default_known_cleanup_is_disabled_and_dry():
    d = svc.safe_default_for("members.cleanup")
    assert d == {"enabled": False, "dryRun": True}


def test_safe_default_known_klarna_disabled():
    assert svc.safe_default_for("members.klarna_reconcile") == {"enabled": False}


def test_safe_default_unknown_key_is_never_enabled():
    # Twarda zasada: nieznany klugel toggle = wylaczone, nigdy enabled=true.
    d = svc.safe_default_for("something.brand_new")
    assert d.get("enabled") is False


async def test_get_setting_no_row_returns_safe_default(monkeypatch):
    async def fake_no_row(key: str):
        return svc.safe_default_for(key)

    monkeypatch.setattr(svc, "_load_fresh", fake_no_row)
    out = await svc.get_setting("members.cleanup")
    assert out == {"enabled": False, "dryRun": True}


async def test_get_setting_partial_row_keeps_safe_missing_fields(monkeypatch):
    # Wiersz {"enabled": true} bez dryRun: brakujace pole dostaje bezpieczny
    # default (dryRun=true), nie znika.
    async def fake_partial(key: str):
        return {**svc.safe_default_for(key), "enabled": True}

    monkeypatch.setattr(svc, "_load_fresh", fake_partial)
    out = await svc.get_setting("members.cleanup")
    assert out == {"enabled": True, "dryRun": True}


async def test_get_effective_db_overrides_env(monkeypatch):
    async def fake(key: str):
        return {"value": "claude-opus-from-db"}

    monkeypatch.setattr(svc, "get_setting", fake)
    val = await svc.get_effective(
        "circle_dm.draft_model", env_fallback="claude-sonnet-env", safe_default=None
    )
    assert val == "claude-opus-from-db"


async def test_get_effective_falls_back_to_env_when_no_row(monkeypatch):
    async def fake(key: str):
        # Brak wiersza -> get_setting zwraca generyczny safe default (bez "value").
        return svc.safe_default_for(key)

    monkeypatch.setattr(svc, "get_setting", fake)
    val = await svc.get_effective(
        "circle_dm.draft_model", env_fallback="claude-sonnet-env", safe_default="x"
    )
    assert val == "claude-sonnet-env"


async def test_get_effective_safe_default_when_no_db_no_env(monkeypatch):
    async def fake(key: str):
        return svc.safe_default_for(key)

    monkeypatch.setattr(svc, "get_setting", fake)
    val = await svc.get_effective("some.tunable", env_fallback=None, safe_default=42)
    assert val == 42
