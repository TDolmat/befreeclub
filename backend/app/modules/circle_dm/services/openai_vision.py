"""Port tools/circle-dm/services/openai-vision.ts - klient OpenAI Vision (httpx).

Chat Completions z obrazkiem jako URL i detail "low" (85 tokenow/obraz).
Bledy 1:1 wg docs/spec/services-media.md sekcja 8. System prompt DOSLOWNIE z TS.
"""

import httpx

from app.core.config import settings
from app.core.logging import create_logger

log = create_logger("vision")

OPENAI_URL = "https://api.openai.com/v1/chat/completions"

OPENAI_TIMEOUT_S = 120.0


class VisionConfigError(Exception):
    pass


class VisionFetchError(Exception):
    pass


class VisionApiError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


SYSTEM_PROMPT = """Opisujesz zdjęcia załączone w wiadomościach DM Be Free Club (community freelancerów AI).
Twoim zadaniem jest dać krótki opis (2-3 zdania) który pozwoli asystentowi AI zrozumieć co użytkownik wysłał.

Zasady:
- Pisz po polsku, krótko i konkretnie
- Jeśli zdjęcie ma tekst (screen czatu, faktura, mockup, screen z aplikacji) zacytuj go w cudzysłowach
- Jeśli to screen rozmowy zaznacz kto napisał co (np. "klient: 'cena?'", "autor: '599 zł'")
- Jeśli to memik/zdjęcie poglądowe opisz krótko temat
- Bez bełkotu typu "to zdjęcie przedstawia" - od razu do treści
- Bez emoji
- Bez myślników długich, używaj kropek"""


async def describe_image_from_url(image_url: str) -> dict:
    """DescribeResult: {"description": str, "tokensUsed": int | None}."""
    if not settings.OPENAI_API_KEY:
        raise VisionConfigError("OPENAI_API_KEY not set")

    body = {
        "model": settings.OPENAI_VISION_MODEL,
        "max_tokens": 300,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Opisz to zdjęcie:"},
                    {"type": "image_url", "image_url": {"url": image_url, "detail": "low"}},
                ],
            },
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=OPENAI_TIMEOUT_S) as client:
            res = await client.post(
                OPENAI_URL,
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except Exception as err:
        raise VisionFetchError(f"fetch openai: {err}") from err

    if not res.is_success:
        err_body = res.text or ""
        raise VisionApiError(f"vision {res.status_code}: {err_body[:400]}", res.status_code)

    json_body = res.json()
    if not isinstance(json_body, dict):
        json_body = {}
    content = None
    choices = json_body.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
    description = content.strip() if isinstance(content, str) else ""
    if not description:
        raise VisionApiError("vision returned empty description", 200)

    usage = json_body.get("usage")
    total = usage.get("total_tokens") if isinstance(usage, dict) else None
    tokens_used = total if isinstance(total, int | float) and not isinstance(total, bool) else None

    log.debug(
        f"described image ({tokens_used if tokens_used is not None else '?'} tokens, "
        f"{len(description)} chars)"
    )
    return {"description": description, "tokensUsed": tokens_used}
