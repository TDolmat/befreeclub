"""Port circle/client.ts - niskopoziomowy klient Circle.so Headless Member API.

Zero retry/backoff - logika 401 -> invalidate_jwt zyje warstwe wyzej.
Timeout httpx NIE jest mapowany na CircleApiError (w TS to AbortError).
"""

import json
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.logging import create_logger
from app.modules.circle_dm.circle.tiptap import text_to_tiptap

__all__ = [
    "CircleApiError",
    "exchange_admin_token_for_jwt",
    "list_threads",
    "get_thread_messages",
    "text_to_tiptap",
    "send_message",
    "mark_chat_room_read",
    "send_to_new_recipient",
    "list_members",
]

log = create_logger("circle")

BASE = "https://app.circle.so"

_UNSET = object()


class CircleApiError(Exception):
    def __init__(self, status: int, body: str, message: str | None = None) -> None:
        if message is None:
            message = f"Circle API {status}: {body[:200]}"
        super().__init__(message)
        self.status = status
        self.body = body
        self.message = message


async def _request(
    method: str,
    path: str,
    *,
    auth: str,
    body: Any = _UNSET,
    timeout_ms: int = 30_000,
) -> Any:
    url = path if path.startswith("http") else f"{BASE}{path}"
    content = (
        None
        if body is _UNSET
        else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    )

    # follow_redirects=True: fetch w Node domyslnie podaza za redirectami (max 20),
    # httpx domyslnie nie - wyrownanie semantyki 1:1.
    async with httpx.AsyncClient(timeout=timeout_ms / 1000, follow_redirects=True) as client:
        res = await client.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {auth}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            content=content,
        )

    text = res.text
    if not res.is_success:
        log.warn(f"{method} {path} → {res.status_code}", text[:200])
        raise CircleApiError(
            res.status_code, text, f"Circle API {res.status_code}: {text[:200]}"
        )

    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        raise CircleApiError(res.status_code, text, "Circle returned non-JSON response") from None


async def exchange_admin_token_for_jwt(admin_token: str, email: str) -> dict:
    """Wymiana admin headless tokena + emaila na ~1h member JWT.

    UWAGA: jedyny endpoint na sciezce /api/v1/headless/... (reszta to /api/headless/v1/...).
    """
    log.debug(f"auth_token exchange for {email}")
    return await _request(
        "POST",
        "/api/v1/headless/auth_token",
        auth=admin_token,
        body={"email": email},
    )


async def list_threads(jwt: str, *, page: int = 1, per_page: int = 50) -> dict:
    return await _request(
        "GET",
        f"/api/headless/v1/messages?page={page}&per_page={per_page}",
        auth=jwt,
    )


async def get_thread_messages(jwt: str, chat_room_uuid: str, *, per_page: int = 100) -> dict:
    return await _request(
        "GET",
        f"/api/headless/v1/messages/{chat_room_uuid}/chat_room_messages?per_page={per_page}",
        auth=jwt,
    )


async def send_message(jwt: str, chat_room_uuid: str, body: str) -> dict:
    """Circle wymaga OBU pol: body (plain) i rich_text_body (pelny envelope),
    inaczej POST przechodzi, ale tresc jest po cichu gubiona."""
    return await _request(
        "POST",
        f"/api/headless/v1/messages/{chat_room_uuid}/chat_room_messages",
        auth=jwt,
        body={
            "body": body,
            "rich_text_body": text_to_tiptap(body),
        },
    )


async def mark_chat_room_read(jwt: str, chat_room_uuid: str) -> None:
    await _request(
        "PATCH",
        f"/api/headless/v1/messages/{chat_room_uuid}",
        auth=jwt,
        body={"unread_messages_count": 0},
    )


async def send_to_new_recipient(jwt: str, community_member_ids: list[int], body: str) -> dict:
    return await _request(
        "POST",
        "/api/headless/v1/messages",
        auth=jwt,
        body={
            "chat_room": {
                "kind": "direct",
                "community_member_ids": community_member_ids,
            },
            "body": body,
            "rich_text_body": text_to_tiptap(body),
        },
    )


async def list_members(
    jwt: str,
    *,
    page: int = 1,
    per_page: int = 100,
    query: str | None = None,
) -> dict:
    params = {"page": str(page), "per_page": str(per_page)}
    if query:
        params["query"] = query
    return await _request(
        "GET",
        f"/api/headless/v1/community_members?{urlencode(params)}",
        auth=jwt,
    )
