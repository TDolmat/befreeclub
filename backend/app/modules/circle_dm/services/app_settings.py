"""Port tools/circle-dm/services/app-settings.ts (1:1, wlacznie z cache 30 s
per proces). Pole cached_at w snapshocie jest TYLKO techniczne (TTL cache) -
GET /settings go nie wystawia (naprawa quirka, docs/spec/port-odstepstwa.md)."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import async_session_maker
from app.modules.circle_dm.models import AppSettings


@dataclass
class AppSettingsSnapshot:
    global_meta_prompt: str
    format_prompt: str
    draft_model: str | None
    format_model: str | None
    no_reply_threshold_days: int
    silence_threshold_days: int
    cached_at: int  # epoch ms; tylko do TTL cache, nie wychodzi w JSON


_cached: AppSettingsSnapshot | None = None
CACHE_MS = 30_000


async def _load_fresh() -> AppSettingsSnapshot:
    global _cached
    async with async_session_maker() as session:
        row = (
            await session.execute(select(AppSettings).where(AppSettings.id == 1).limit(1))
        ).scalar_one_or_none()
    _cached = AppSettingsSnapshot(
        global_meta_prompt=row.global_meta_prompt if row else "",
        format_prompt=row.format_prompt if row else "",
        draft_model=row.draft_model if row else None,
        format_model=row.format_model if row else None,
        no_reply_threshold_days=row.no_reply_threshold_days if row else 3,
        silence_threshold_days=row.silence_threshold_days if row else 14,
        cached_at=int(time.time() * 1000),
    )
    return _cached


async def get_settings() -> AppSettingsSnapshot:
    if _cached and time.time() * 1000 - _cached.cached_at < CACHE_MS:
        return _cached
    return await _load_fresh()


async def get_global_meta_prompt() -> str:
    return (await get_settings()).global_meta_prompt


async def get_format_prompt() -> str:
    return (await get_settings()).format_prompt


async def get_draft_model() -> str | None:
    return (await get_settings()).draft_model


async def get_format_model() -> str | None:
    return (await get_settings()).format_model


_MODEL_FIELDS = ("draft_model", "format_model")


async def _upsert_string_field(field: str, value: str | None) -> None:
    global _cached
    # Pusty string dla modeli nadpisalby fallback na env - koercja do NULL.
    if field in _MODEL_FIELDS and (value is None or value == ""):
        normalized: str | None = None
    else:
        normalized = value if value is not None else ""
    now = datetime.now(UTC)
    stmt = pg_insert(AppSettings).values(id=1, **{field: normalized}, updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSettings.id], set_={field: normalized, "updated_at": now}
    )
    async with async_session_maker() as session:
        await session.execute(stmt)
        await session.commit()
    _cached = None


async def _upsert_int_field(field: str, value: int) -> None:
    global _cached
    now = datetime.now(UTC)
    stmt = pg_insert(AppSettings).values(id=1, **{field: value}, updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSettings.id], set_={field: value, "updated_at": now}
    )
    async with async_session_maker() as session:
        await session.execute(stmt)
        await session.commit()
    _cached = None


async def set_global_meta_prompt(value: str) -> None:
    await _upsert_string_field("global_meta_prompt", value)


async def set_format_prompt(value: str) -> None:
    await _upsert_string_field("format_prompt", value)


async def set_draft_model(value: str | None) -> None:
    await _upsert_string_field("draft_model", value)


async def set_format_model(value: str | None) -> None:
    await _upsert_string_field("format_model", value)


async def set_no_reply_threshold_days(value: int) -> None:
    await _upsert_int_field("no_reply_threshold_days", value)


async def set_silence_threshold_days(value: int) -> None:
    await _upsert_int_field("silence_threshold_days", value)


def compose_system_prompt(persona: str, meta_prompt: str) -> str:
    """Dla generowania draftow (draft- i compose-orchestrator)."""
    if not meta_prompt.strip():
        return persona
    return f"[GLOBALNE ZASADY STYLU — stosuj zawsze]\n{meta_prompt.strip()}\n\n---\n\n{persona}"


def compose_format_system_prompt(persona: str, meta_prompt: str, format_prompt: str) -> str:
    """Dla "Formatuj z AI" (format-orchestrator)."""
    base = compose_system_prompt(persona, meta_prompt)
    if not format_prompt.strip():
        return base
    return f"{base}\n\n---\n\n[INSTRUKCJA FORMATOWANIA]\n{format_prompt.strip()}"
