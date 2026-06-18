"""Testy klienta Sender.net (create -> fallback PATCH, sanityzacja tokenu)."""

import json

import httpx
import pytest
import respx

from app.core.config import settings
from app.modules.newsletter.services import sender

CREATE_URL = "https://api.sender.net/v2/subscribers"
UPDATE_URL = "https://api.sender.net/v2/subscribers/jan%40x.pl"


@pytest.fixture
def sender_configured(monkeypatch):
    monkeypatch.setattr(settings, "SENDER_API_TOKEN", "sender-tok")
    monkeypatch.setattr(settings, "SENDER_GROUP_IDS", None)


def test_api_token_sanitized(monkeypatch):
    monkeypatch.setattr(settings, "SENDER_API_TOKEN", "  Bearer  tok-123 ")
    assert sender.api_token() == "tok-123"
    monkeypatch.setattr(settings, "SENDER_API_TOKEN", "bearer tok-456")
    assert sender.api_token() == "tok-456"
    monkeypatch.setattr(settings, "SENDER_API_TOKEN", "tok-789")
    assert sender.api_token() == "tok-789"
    monkeypatch.setattr(settings, "SENDER_API_TOKEN", None)
    assert sender.api_token() == ""
    assert not sender.is_configured()


def test_group_ids_default_and_custom(monkeypatch):
    monkeypatch.setattr(settings, "SENDER_GROUP_IDS", None)
    assert sender.group_ids() == ["epnLzm", "el06vl"]
    monkeypatch.setattr(settings, "SENDER_GROUP_IDS", " aB1 , ,cD2 ")
    assert sender.group_ids() == ["aB1", "cD2"]


@respx.mock
async def test_push_subscriber_create_ok(sender_configured):
    route = respx.post(CREATE_URL).mock(return_value=httpx.Response(200, json={"data": {}}))

    assert await sender.push_subscriber("jan@x.pl", "Jan") is True

    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer sender-tok"
    assert request.headers["accept"] == "application/json"
    assert json.loads(request.content) == {
        "email": "jan@x.pl",
        "firstname": "Jan",
        "groups": ["epnLzm", "el06vl"],
        "trigger_automation": True,
    }


@respx.mock
async def test_push_subscriber_fallback_patch(sender_configured):
    respx.post(CREATE_URL).mock(
        return_value=httpx.Response(422, json={"message": "Subscriber already exists"})
    )
    update_route = respx.patch(UPDATE_URL).mock(return_value=httpx.Response(200, json={}))

    assert await sender.push_subscriber("jan@x.pl", "Jan") is True

    request = update_route.calls.last.request
    body = json.loads(request.content)
    # PATCH bez pola email (1:1 z oryginalem), email tylko w URL (encodeURIComponent).
    assert "email" not in body
    assert body == {"firstname": "Jan", "groups": ["epnLzm", "el06vl"], "trigger_automation": True}


@respx.mock
async def test_push_subscriber_both_fail(sender_configured):
    respx.post(CREATE_URL).mock(return_value=httpx.Response(422, json={"message": "nope"}))
    respx.patch(UPDATE_URL).mock(return_value=httpx.Response(500, text="boom"))

    assert await sender.push_subscriber("jan@x.pl", "Jan") is False


@respx.mock
async def test_push_subscriber_network_error(sender_configured):
    respx.post(CREATE_URL).mock(side_effect=httpx.ConnectError("boom"))

    assert await sender.push_subscriber("jan@x.pl", "Jan") is False
