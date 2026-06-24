"""Port tools/circle-dm/services/openai-stt.ts - klient OpenAI Whisper (httpx).

Bledy 1:1 wg docs/spec/services-media.md sekcja 7. Roznica vs TS: jawne timeouty
(Node fetch nie mial zadnego) - timeout/blad sieci POST do OpenAI mapowany na
SttFetchError (nie-fatalny, worker retry'uje).
"""

import math

import httpx

from app.core.logging import create_logger
from app.modules.admin.services import secrets, settings_catalog

log = create_logger("stt")

# Twardy limit Whispera: 25 MB na plik. Glosowki Circle (AAC/mp4) to 1-2 MB.
MAX_BYTES = 25 * 1024 * 1024

OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"

AUDIO_FETCH_TIMEOUT_S = 60.0
OPENAI_TIMEOUT_S = 120.0


class SttConfigError(Exception):
    pass


class SttFetchError(Exception):
    pass


class SttApiError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _guess_filename(content_type: str | None) -> str:
    if not content_type:
        return "voice.m4a"
    if "mp4" in content_type or "m4a" in content_type:
        return "voice.m4a"
    if "mpeg" in content_type:
        return "voice.mp3"
    if "webm" in content_type:
        return "voice.webm"
    if "ogg" in content_type:
        return "voice.ogg"
    if "wav" in content_type:
        return "voice.wav"
    return "voice.m4a"


async def transcribe_audio_from_url(
    audio_url: str,
    *,
    filename: str | None = None,
    language: str | None = "pl",
) -> dict:
    """TranscribeResult: {"text": str, "durationSec": int | None, "language": str | None}.

    `language`: default "pl" (twardy hint jak w TS, worker nie podaje wlasnego);
    None = pole pominiete (autodetekcja); string = przekazany dalej.
    """
    api_key = secrets.resolve_sync("openai.api_key", env_fallback=True)
    if not api_key:
        raise SttConfigError("OPENAI_API_KEY not set")

    # Podpisane URL-e Active Storage Circle sa publicznie pobieralne - bez auth.
    try:
        async with httpx.AsyncClient(
            timeout=AUDIO_FETCH_TIMEOUT_S, follow_redirects=True
        ) as client:
            audio_res = await client.get(audio_url)
    except Exception as err:
        raise SttFetchError(f"fetch {audio_url}: {err}") from err
    if not audio_res.is_success:
        raise SttFetchError(f"fetch {audio_url}: HTTP {audio_res.status_code}")
    buf = audio_res.content
    if len(buf) == 0:
        raise SttFetchError(f"fetch {audio_url}: empty body")
    if len(buf) > MAX_BYTES:
        raise SttFetchError(f"audio too large for Whisper ({len(buf)} B > {MAX_BYTES} B)")

    ct_header = audio_res.headers.get("content-type")
    fname = filename if filename is not None else _guess_filename(ct_header)
    content_type = ct_header if ct_header is not None else "application/octet-stream"

    model = await settings_catalog.effective("openaiWhisperModel")
    data: dict[str, str] = {
        "model": model,
        "response_format": "verbose_json",
    }
    if language:
        data["language"] = language

    try:
        async with httpx.AsyncClient(timeout=OPENAI_TIMEOUT_S) as client:
            stt_res = await client.post(
                OPENAI_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (fname, buf, content_type)},
                data=data,
            )
    except httpx.HTTPError as err:
        raise SttFetchError(f"fetch openai: {err}") from err

    if not stt_res.is_success:
        err_body = stt_res.text or ""
        raise SttApiError(
            f"whisper {stt_res.status_code}: {err_body[:400]}", stt_res.status_code
        )

    json_body = stt_res.json()
    if not isinstance(json_body, dict):
        json_body = {}
    raw_text = json_body.get("text")
    text = raw_text.strip() if isinstance(raw_text, str) else ""
    if not text:
        raise SttApiError("whisper returned empty text", 200)
    raw_duration = json_body.get("duration")
    duration_sec = (
        # Math.round z JS = floor(x + 0.5), nie bankers rounding Pythona.
        int(math.floor(raw_duration + 0.5))
        if isinstance(raw_duration, int | float) and not isinstance(raw_duration, bool)
        else None
    )
    raw_language = json_body.get("language")
    lang = raw_language if isinstance(raw_language, str) else None

    log.debug(f"transcribed {fname} ({len(buf)}B, ~{duration_sec}s)")

    return {"text": text, "durationSec": duration_sec, "language": lang}
