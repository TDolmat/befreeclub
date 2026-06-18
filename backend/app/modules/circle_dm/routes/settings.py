"""Port tools/circle-dm/routes/settings.ts (1:1 wg docs/spec/routes-a.md sekcja 6).
Montowane pod /api/circle-dm/settings (za require_auth).

Quirk 1:1: PUT zapisuje kazde pole OSOBNYM upsertem (nie-atomowo) w stalej
kolejnosci. Swiadoma naprawa: GET NIE wystawia juz technicznego pola cachedAt
(oryginal wyciekal epoch ms ze snapshotu cache, front go nie czyta).
"""

from fastapi import APIRouter

from app.modules.circle_dm.schemas import UpdateSettingsBody
from app.modules.circle_dm.services import app_settings

router = APIRouter()


@router.get("")
async def get_settings() -> dict:
    s = await app_settings.get_settings()
    return {
        "globalMetaPrompt": s.global_meta_prompt,
        "formatPrompt": s.format_prompt,
        "draftModel": s.draft_model,
        "formatModel": s.format_model,
        "noReplyThresholdDays": s.no_reply_threshold_days,
        "silenceThresholdDays": s.silence_threshold_days,
    }


@router.put("")
async def update_settings(payload: UpdateSettingsBody) -> dict:
    provided = payload.model_fields_set
    if "global_meta_prompt" in provided:
        await app_settings.set_global_meta_prompt(payload.global_meta_prompt)
    if "format_prompt" in provided:
        await app_settings.set_format_prompt(payload.format_prompt)
    if "draft_model" in provided:
        await app_settings.set_draft_model(payload.draft_model)
    if "format_model" in provided:
        await app_settings.set_format_model(payload.format_model)
    if "no_reply_threshold_days" in provided:
        await app_settings.set_no_reply_threshold_days(payload.no_reply_threshold_days)
    if "silence_threshold_days" in provided:
        await app_settings.set_silence_threshold_days(payload.silence_threshold_days)
    return {"ok": True}
