"""Centralne API ustawien panelu admina (GET /api/admin/settings, PUT /{group}).

Bez DB: store (admin.settings) jest mockowany - in-memory dict - zeby suite nie
zalezal od Postgresa (jak test_admin_settings.py). Weryfikujemy:
- GET zwraca efektywne wartosci pogrupowane sekcjami (DB > env > safe default),
- PUT zmienia knob i zwraca stan grupy,
- value:null przywraca fallback env (usuwa nadpisanie),
- zly typ / zakres / nieznany klucz / nieznana grupa -> 400/404,
- TOGGLE membership: bezpieczne domysle (enabled=false), zapis enabled/dryRun,
- knoby circle_dm prompty/modele NIE sa duplikowane w centralnym API
  (jedno zrodlo prawdy = /api/circle-dm/settings).
"""

import httpx
import pytest

from app.core.config import settings as env
from app.modules.admin.services import settings as store
from app.modules.admin.services import settings_catalog as catalog


@pytest.fixture
def fake_store(monkeypatch):
    """In-memory podmiana admin.settings store. Zwraca dict rows (store_key -> value)."""
    rows: dict[str, dict] = {}

    async def fake_get_setting(key: str):
        if key in rows:
            return dict(rows[key])
        return store.safe_default_for(key)

    async def fake_set_setting(key: str, value: dict, user_id):
        rows[key] = dict(value)
        return dict(rows[key])

    monkeypatch.setattr(store, "get_setting", fake_get_setting)
    monkeypatch.setattr(store, "set_setting", fake_set_setting)
    return rows


@pytest.fixture
async def client(monkeypatch, fake_store):
    monkeypatch.setattr(env, "NODE_ENV", "development")  # require_auth -> DEV_FAKE_AUTH
    from app.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


# ── Serwis katalogu (bez HTTP) ──────────────────────────────────────────────


async def test_build_all_groups_has_four_sections(fake_store):
    out = await catalog.build_all_groups()
    assert set(out["groups"].keys()) == {
        "circleDmAi",
        "membership",
        "billingNewsletter",
        "analytics",
    }


async def test_tunable_effective_falls_back_to_env_when_no_db(fake_store):
    out = await catalog.build_all_groups()
    kb = out["groups"]["circleDmAi"]["kbBudgetTokens"]
    assert kb["value"] == env.KB_BUDGET_TOKENS
    assert kb["source"] == "env"
    assert kb["requiresRestart"] is False


async def test_db_override_wins_over_env(fake_store):
    fake_store["circle_dm.kb_budget_tokens"] = {"value": 80000}
    out = await catalog.build_all_groups()
    kb = out["groups"]["circleDmAi"]["kbBudgetTokens"]
    assert kb["value"] == 80000
    assert kb["source"] == "db"


async def test_membership_toggle_safe_default_disabled(fake_store):
    out = await catalog.build_all_groups()
    cleanup = out["groups"]["membership"]["cleanup"]
    assert cleanup == {"enabled": False, "dryRun": True, "destructive": True}


async def test_apply_patch_null_restores_env(fake_store):
    await catalog.apply_patch("circleDmAi", {"kbBudgetTokens": {"value": 80000}}, None)
    assert fake_store["circle_dm.kb_budget_tokens"] == {"value": 80000}
    # null -> wiersz bez "value" -> efektywnie env.
    state = await catalog.apply_patch("circleDmAi", {"kbBudgetTokens": {"value": None}}, None)
    assert "value" not in fake_store["circle_dm.kb_budget_tokens"]
    assert state["kbBudgetTokens"]["value"] == env.KB_BUDGET_TOKENS
    assert state["kbBudgetTokens"]["source"] == "env"


async def test_apply_patch_rejects_bad_type(fake_store):
    with pytest.raises(catalog.SettingsValidationError):
        await catalog.apply_patch("circleDmAi", {"kbBudgetTokens": {"value": "lots"}}, None)


async def test_apply_patch_rejects_out_of_range(fake_store):
    with pytest.raises(catalog.SettingsValidationError):
        await catalog.apply_patch("circleDmAi", {"claudeMaxConcurrent": {"value": 99}}, None)


async def test_apply_patch_rejects_interval_below_min(fake_store):
    with pytest.raises(catalog.SettingsValidationError):
        await catalog.apply_patch("circleDmAi", {"pollingIntervalMs": {"value": 100}}, None)


async def test_apply_patch_rejects_unknown_key(fake_store):
    with pytest.raises(catalog.SettingsValidationError):
        await catalog.apply_patch("circleDmAi", {"nopeKey": {"value": 1}}, None)


async def test_apply_patch_toggle_writes_enabled_and_dryrun(fake_store):
    state = await catalog.apply_patch(
        "membership", {"cleanup": {"enabled": True, "dryRun": False}}, None
    )
    assert fake_store["members.cleanup"] == {"enabled": True, "dryRun": False}
    assert state["cleanup"] == {"enabled": True, "dryRun": False, "destructive": True}


async def test_apply_patch_toggle_rejects_non_bool(fake_store):
    with pytest.raises(catalog.SettingsValidationError):
        await catalog.apply_patch("membership", {"cleanup": {"enabled": "yes"}}, None)


async def test_circle_dm_prompts_not_in_central_catalog(fake_store):
    """Jedno zrodlo prawdy: prompty/modele draft+format zyja w circle_dm.settings,
    centralne API ich NIE wystawia (link do /api/circle-dm/settings)."""
    out = await catalog.build_all_groups()
    circle = out["groups"]["circleDmAi"]
    for forbidden in ("globalMetaPrompt", "formatPrompt", "draftModel", "formatModel",
                      "noReplyThresholdDays", "silenceThresholdDays"):
        assert forbidden not in circle


# ── Route HTTP ───────────────────────────────────────────────────────────────


async def test_get_settings_route(client):
    res = await client.get("/api/admin/settings")
    assert res.status_code == 200
    body = res.json()
    assert "groups" in body
    assert body["groups"]["circleDmAi"]["kbBudgetTokens"]["value"] == env.KB_BUDGET_TOKENS


async def test_put_changes_value_via_route(client, fake_store):
    res = await client.put(
        "/api/admin/settings/circleDmAi", json={"kbBudgetTokens": {"value": 70000}}
    )
    assert res.status_code == 200
    assert res.json()["kbBudgetTokens"]["value"] == 70000
    assert res.json()["kbBudgetTokens"]["source"] == "db"
    assert fake_store["circle_dm.kb_budget_tokens"] == {"value": 70000}


async def test_put_bad_type_is_400(client):
    res = await client.put(
        "/api/admin/settings/circleDmAi", json={"kbBudgetTokens": {"value": "nope"}}
    )
    assert res.status_code == 400
    # Komunikat walidacji odmaskowany - panel mapuje realny powod na podpowiedz.
    # Nie asertujemy dokladnego stringa, tylko ze niesie konkret (typ wartosci).
    error = res.json()["error"]
    assert "integer" in error


async def test_put_out_of_range_is_400_with_reason(client):
    """Odmaskowana walidacja: PUT poza zakresem zwraca KONKRETNY powod, nie staly
    'Invalid request' (panel mapuje realny powod na podpowiedz). claudeMaxConcurrent
    ma zakres 1-8, wiec 99 leci 'must be <= 8'."""
    res = await client.put(
        "/api/admin/settings/circleDmAi", json={"claudeMaxConcurrent": {"value": 99}}
    )
    assert res.status_code == 400
    error = res.json()["error"]
    assert error != "Invalid request"
    assert "8" in error


async def test_put_unknown_group_is_404(client):
    res = await client.put("/api/admin/settings/doesNotExist", json={"x": {"value": 1}})
    assert res.status_code == 404


async def test_put_then_get_reflects_change(client, fake_store):
    await client.put(
        "/api/admin/settings/circleDmAi", json={"openaiVisionModel": {"value": "gpt-4o"}}
    )
    res = await client.get("/api/admin/settings")
    assert res.json()["groups"]["circleDmAi"]["openaiVisionModel"]["value"] == "gpt-4o"
