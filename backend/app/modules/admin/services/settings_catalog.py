"""Katalog edytowalnych knobow panelu admina + budowa odpowiedzi GET / patch PUT.

Zrodlo prawdy dla kontraktu: docs/spec-landing/ustawienia-katalog.md (sekcja 7).
Kazdy knob to deskryptor (TUNABLE albo TOGGLE) z kluczem admin.settings, fallbackiem
z env (app.core.config.settings), bezpiecznym defaultem, walidacja i flaga
requiresRestart. Route iteruje ten katalog - dodajesz knob = dopisujesz deskryptor,
nie dotykasz route'a.

Sekrety NIE sa tu wystawiane (status-only, osobny endpoint /api/admin/connections).
Prompty/modele/progi circle_dm maja WLASNY storage (circle_dm.settings, serwis
circle_dm/services/app_settings.py, API /api/circle-dm/settings) - NIE duplikujemy
ich w admin.settings. Grupa circleDmAi tutaj niesie wylacznie knoby, ktore dzis sa
TYLKO w env (semafory, interwaly workerow, limity KB, modele OpenAI).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.config import settings as env
from app.modules.admin.services import settings as store

GroupName = Literal["circleDmAi", "membership", "billingNewsletter", "analytics"]


class SettingsValidationError(ValueError):
    """Niepoprawny patch (zly typ / zakres). Route zamienia na 400 {"error":...}."""


# ── TUNABLE: skalar trzymany jako {"value": ...}, get_effective(DB > env > default) ──


@dataclass(frozen=True)
class Tunable:
    """Skalarny knob nadpisujacy env. Zapis: admin.settings[key] = {"value": <scalar|null>}."""

    json_key: str  # camelCase klucz w API (np. "kbBudgetTokens")
    store_key: str  # klucz admin.settings (np. "circle_dm.kb_budget_tokens")
    env_attr: str | None  # pole app.core.config.settings (None = brak env, tylko default)
    safe_default: Any  # gdy brak DB i brak env
    requires_restart: bool
    kind: Literal["int", "str"]
    coerce: Callable[[Any], Any]  # walidacja + koercja; rzuca SettingsValidationError


def _int_in_range(min_v: int | None = None, max_v: int | None = None) -> Callable[[Any], int]:
    def _coerce(raw: Any) -> int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise SettingsValidationError("expected integer")
        if min_v is not None and raw < min_v:
            raise SettingsValidationError(f"must be >= {min_v}")
        if max_v is not None and raw > max_v:
            raise SettingsValidationError(f"must be <= {max_v}")
        return raw

    return _coerce


def _nonempty_str(raw: Any) -> str:
    if not isinstance(raw, str) or raw.strip() == "":
        raise SettingsValidationError("expected non-empty string")
    return raw


def _optional_str(raw: Any) -> str | None:
    # null/"" przywraca fallback env (usuwa nadpisanie - serwis traktuje null jak brak).
    if raw is None or (isinstance(raw, str) and raw == ""):
        return None
    if not isinstance(raw, str):
        raise SettingsValidationError("expected string or null")
    return raw


def _url_str(raw: Any) -> str:
    s = _nonempty_str(raw)
    if "://" not in s:
        raise SettingsValidationError("must be a valid URL")
    return s


# ── TOGGLE: bramka workera, {"enabled":..., "dryRun"?:...}, get_setting + SAFE_DEFAULTS ──


@dataclass(frozen=True)
class Toggle:
    json_key: str
    store_key: str
    destructive: bool = False
    has_dry_run: bool = False


# ── Definicje grup ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Group:
    tunables: list[Tunable] = field(default_factory=list)
    toggles: list[Toggle] = field(default_factory=list)


CATALOG: dict[GroupName, Group] = {
    # Knoby AI/Circle DM dzis tylko w env. Prompty/modele draft+format i progi
    # no_reply/silence NIE tu - zyja w circle_dm.settings (link do /api/circle-dm/settings).
    "circleDmAi": Group(
        tunables=[
            Tunable(
                "claudeMaxConcurrent", "circle_dm.claude_max_concurrent",
                "CLAUDE_MAX_CONCURRENT", 2, True, "int", _int_in_range(1, 8),
            ),
            Tunable(
                "pollingIntervalMs", "circle_dm.polling_interval_ms",
                "POLLING_INTERVAL_MS", 30000, True, "int", _int_in_range(5000),
            ),
            Tunable(
                "voiceTranscriptIntervalMs", "circle_dm.voice_transcript_interval_ms",
                "VOICE_TRANSCRIPT_INTERVAL_MS", 20000, True, "int", _int_in_range(5000),
            ),
            Tunable(
                "imageDescriptionIntervalMs", "circle_dm.image_description_interval_ms",
                "IMAGE_DESCRIPTION_INTERVAL_MS", 20000, True, "int", _int_in_range(5000),
            ),
            Tunable(
                "kbBudgetTokens", "circle_dm.kb_budget_tokens",
                "KB_BUDGET_TOKENS", 60000, False, "int", _int_in_range(1),
            ),
            Tunable(
                "kbHardCeilingTokens", "circle_dm.kb_hard_ceiling_tokens",
                "KB_HARD_CEILING_TOKENS", 90000, False, "int", _int_in_range(1),
            ),
            Tunable(
                "openaiWhisperModel", "circle_dm.openai_whisper_model",
                "OPENAI_WHISPER_MODEL", "whisper-1", False, "str", _nonempty_str,
            ),
            Tunable(
                "openaiVisionModel", "circle_dm.openai_vision_model",
                "OPENAI_VISION_MODEL", "gpt-4o-mini", False, "str", _nonempty_str,
            ),
        ],
    ),
    # Bramki destrukcyjnych workerow + ich interwaly (sekcja 2 katalogu).
    "membership": Group(
        toggles=[
            Toggle("cleanup", "members.cleanup", destructive=True, has_dry_run=True),
            Toggle("klarnaReconcile", "members.klarna_reconcile"),
            Toggle("inviteRetry", "members.invite_retry"),
        ],
        tunables=[
            Tunable(
                "cleanupIntervalMs", "members.cleanup_interval_ms",
                "MEMBERSHIP_CLEANUP_INTERVAL_MS", 21_600_000, True, "int", _int_in_range(5000),
            ),
            Tunable(
                "klarnaReconcileIntervalMs", "members.klarna_reconcile_interval_ms",
                "KLARNA_RECONCILE_INTERVAL_MS", 3_600_000, True, "int", _int_in_range(5000),
            ),
            Tunable(
                "inviteRetryIntervalMs", "members.invite_retry_interval_ms",
                "INVITE_RETRY_INTERVAL_MS", 3_600_000, True, "int", _int_in_range(5000),
            ),
        ],
    ),
    "billingNewsletter": Group(
        tunables=[
            Tunable(
                "frontendUrl", "billing.frontend_url",
                "FRONTEND_URL", "https://befreeclub.pl", False, "str", _url_str,
            ),
            Tunable(
                "confirmUrlBase", "newsletter.confirm_url_base",
                "CONFIRM_URL_BASE", "https://befreeclub.pl/newsletter/potwierdz",
                False, "str", _url_str,
            ),
            Tunable(
                "cancellationFromEmail", "billing.cancellation_from_email",
                "CANCELLATION_FROM_EMAIL", "Be Free Club <noreply@befreeclub.pl>",
                False, "str", _nonempty_str,
            ),
            Tunable(
                "newsletterFromEmail", "newsletter.from_email",
                "NEWSLETTER_FROM_EMAIL", "Be Free Club <krystian@befreeclub.pl>",
                False, "str", _nonempty_str,
            ),
            Tunable(
                "senderGroupIds", "newsletter.sender_group_ids",
                "SENDER_GROUP_IDS", "epnLzm,el06vl", False, "str", _nonempty_str,
            ),
            Tunable(
                "ebookFilePath", "billing.ebook_file_path",
                "EBOOK_FILE_PATH", None, False, "str", _optional_str,
            ),
        ],
    ),
    "analytics": Group(
        tunables=[
            Tunable(
                "metaPixelId", "analytics.meta_pixel_id",
                "META_PIXEL_ID", None, False, "str", _optional_str,
            ),
            Tunable(
                "circleCommunityId", "members.circle_community_id",
                "CIRCLE_COMMUNITY_ID", None, False, "str", _optional_str,
            ),
        ],
    ),
}


# ── Odczyt efektywnej wartosci pojedynczego TUNABLE (DB > env > safe_default) ────


async def get_effective_tunable(t: Tunable) -> Any:
    """Efektywna wartosc knoba. Re-eksport reguly precedencji store.get_effective
    z env z app.core.config.settings - jeden punkt dla route i konsumentow."""
    env_fallback = getattr(env, t.env_attr) if t.env_attr else None
    return await store.get_effective(
        t.store_key, env_fallback=env_fallback, safe_default=t.safe_default
    )


# Indeks knobow po json_key (do akcesorow konsumenckich ponizej).
_BY_KEY: dict[str, Tunable] = {
    t.json_key: t for group in CATALOG.values() for t in group.tunables
}


async def effective(json_key: str) -> Any:
    """Efektywna wartosc knoba po jego camelCase json_key. Dla konsumentow
    (workery, serwisy AI) ktorzy chca DB-nadpisanie env bez znajomosci store_key.
    DB > env > safe_default; brak ustawien = dotychczasowa wartosc env."""
    return await get_effective_tunable(_BY_KEY[json_key])


def effective_sync(json_key: str) -> Any:
    """Synchroniczny odpowiednik effective() dla call-site'ow, ktore nie sa
    async (sender.group_ids, circle._credentials itp.). Czyta z tego samego
    procesowego cache co wersja async; zimny cache = env fallback (zachowanie
    1:1 z dotychczasowym), kolejne wywolanie async ogrzewa cache. DB > env >
    safe_default. NIGDY nie blokuje na DB."""
    t = _BY_KEY[json_key]
    env_fallback = getattr(env, t.env_attr) if t.env_attr else None
    return store.get_effective_sync(
        t.store_key, env_fallback=env_fallback, safe_default=t.safe_default
    )


async def _tunable_state(t: Tunable) -> dict[str, Any]:
    raw = await store.get_setting(t.store_key)
    has_db = isinstance(raw, dict) and "value" in raw
    env_fallback = getattr(env, t.env_attr) if t.env_attr else None
    if has_db:
        value, source = raw["value"], "db"
    elif env_fallback is not None:
        value, source = env_fallback, "env"
    else:
        value, source = t.safe_default, "default"
    return {
        "value": value,
        "source": source,
        "envFallback": env_fallback,
        "requiresRestart": t.requires_restart,
    }


async def _toggle_state(t: Toggle) -> dict[str, Any]:
    raw = await store.get_setting(t.store_key)
    out: dict[str, Any] = {"enabled": bool(raw.get("enabled", False))}
    if t.has_dry_run:
        out["dryRun"] = bool(raw.get("dryRun", True))
    if t.destructive:
        out["destructive"] = True
    return out


# ── GET: caly katalog z efektywnymi wartosciami ─────────────────────────────────


async def build_all_groups() -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for name, group in CATALOG.items():
        entries: dict[str, Any] = {}
        for tog in group.toggles:
            entries[tog.json_key] = await _toggle_state(tog)
        for tun in group.tunables:
            entries[tun.json_key] = await _tunable_state(tun)
        groups[name] = entries
    return {"groups": groups}


async def build_group(name: GroupName) -> dict[str, Any]:
    group = CATALOG[name]
    entries: dict[str, Any] = {}
    for tog in group.toggles:
        entries[tog.json_key] = await _toggle_state(tog)
    for tun in group.tunables:
        entries[tun.json_key] = await _tunable_state(tun)
    return entries


# ── PUT: czesciowy patch jednej grupy ──────────────────────────────────────────


async def apply_patch(name: GroupName, body: dict[str, Any], user_id: int | None) -> dict[str, Any]:
    """Waliduje i zapisuje kazdy podany knob. Pomijasz klucz = bez zmian.
    Rzuca SettingsValidationError przy nieznanym kluczu / zlym typie / zakresie.
    Zwraca stan grupy po zapisie (ten sam ksztalt co build_group)."""
    if not isinstance(body, dict) or not body:
        raise SettingsValidationError("empty patch")

    group = CATALOG[name]
    tunables = {t.json_key: t for t in group.tunables}
    toggles = {t.json_key: t for t in group.toggles}

    for json_key, patch in body.items():
        if not isinstance(patch, dict):
            raise SettingsValidationError(f"{json_key}: expected object")
        if json_key in tunables:
            await _apply_tunable(tunables[json_key], patch, user_id)
        elif json_key in toggles:
            await _apply_toggle(toggles[json_key], patch, user_id)
        else:
            raise SettingsValidationError(f"unknown key: {json_key}")

    return await build_group(name)


async def _apply_tunable(t: Tunable, patch: dict[str, Any], user_id: int | None) -> None:
    if "value" not in patch:
        raise SettingsValidationError(f"{t.json_key}: missing 'value'")
    raw = patch["value"]
    if raw is None:
        # null przywraca fallback env: zapisujemy wiersz BEZ pola "value", zeby
        # store.get_effective ("value" in raw) zszedl na env. Pusty dict daje to
        # czysto - merge z safe_default nie wprowadza klucza "value".
        await store.set_setting(t.store_key, {}, user_id)
        return
    await store.set_setting(t.store_key, {"value": t.coerce(raw)}, user_id)


async def _apply_toggle(t: Toggle, patch: dict[str, Any], user_id: int | None) -> None:
    if "enabled" not in patch or not isinstance(patch["enabled"], bool):
        raise SettingsValidationError(f"{t.json_key}: 'enabled' must be boolean")
    value: dict[str, Any] = {"enabled": patch["enabled"]}
    if t.has_dry_run:
        dry = patch.get("dryRun", True)
        if not isinstance(dry, bool):
            raise SettingsValidationError(f"{t.json_key}: 'dryRun' must be boolean")
        value["dryRun"] = dry
    await store.set_setting(t.store_key, value, user_id)
