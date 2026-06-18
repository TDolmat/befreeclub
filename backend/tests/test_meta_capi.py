import hashlib
import json

import httpx
import pytest
import respx

from app.core.config import settings
from app.core.meta_capi import CapiCustomData, CapiUserData, hash_email, send_event

PIXEL_ID = "963496946601553"
EVENTS_URL = f"https://graph.facebook.com/v21.0/{PIXEL_ID}/events"


@pytest.fixture
def capi_enabled(monkeypatch):
    monkeypatch.setattr(settings, "META_PIXEL_ID", PIXEL_ID)
    monkeypatch.setattr(settings, "META_CAPI_TOKEN", "test-capi-token")


def test_hash_email_normalizes_before_sha256():
    expected = hashlib.sha256(b"jan@x.pl").hexdigest()
    assert hash_email("  Jan@X.PL ") == expected
    assert hash_email("jan@x.pl") == expected


@respx.mock
async def test_send_event_posts_full_payload(capi_enabled):
    route = respx.post(EVENTS_URL).mock(
        return_value=httpx.Response(200, json={"events_received": 1})
    )

    ok = await send_event(
        event_name="Purchase",
        event_id="pi_123",
        event_time=1765432100,
        user_data=CapiUserData(
            email="  Jan@X.PL ",
            fbc="fb.1.1700000000000.IwAR123",
            fbp="fb.1.1700000000000.999",
            client_ip="1.2.3.4",
            client_ua="Mozilla/5.0",
        ),
        custom_data=CapiCustomData(value=879.0, currency="pln", content_name="semiannual"),
        event_source_url="https://befreeclub.pl/sukces",
    )

    assert ok is True
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["access_token"] == "test-capi-token"
    event = body["data"][0]
    assert event["event_name"] == "Purchase"
    assert event["event_id"] == "pi_123"
    assert event["event_time"] == 1765432100
    assert event["action_source"] == "website"
    assert event["event_source_url"] == "https://befreeclub.pl/sukces"
    assert event["user_data"]["em"] == [hashlib.sha256(b"jan@x.pl").hexdigest()]
    assert event["user_data"]["fbc"] == "fb.1.1700000000000.IwAR123"
    assert event["user_data"]["fbp"] == "fb.1.1700000000000.999"
    assert event["user_data"]["client_ip_address"] == "1.2.3.4"
    assert event["user_data"]["client_user_agent"] == "Mozilla/5.0"
    assert event["custom_data"] == {
        "value": 879.0,
        "currency": "pln",
        "content_name": "semiannual",
    }


@respx.mock
async def test_send_event_omits_empty_optional_fields(capi_enabled):
    route = respx.post(EVENTS_URL).mock(
        return_value=httpx.Response(200, json={"events_received": 1})
    )

    ok = await send_event(
        event_name="Lead",
        event_id="lead-abc",
        event_time=1765432100,
        user_data=CapiUserData(email="jan@x.pl"),
    )

    assert ok is True
    event = json.loads(route.calls.last.request.content)["data"][0]
    assert "custom_data" not in event
    assert "event_source_url" not in event
    assert set(event["user_data"].keys()) == {"em"}


@respx.mock
async def test_send_event_disabled_without_config(monkeypatch):
    monkeypatch.setattr(settings, "META_PIXEL_ID", None)
    monkeypatch.setattr(settings, "META_CAPI_TOKEN", None)
    # Brak zarejestrowanych route'ow respx: kazdy realny request by wybuchl.
    ok = await send_event(
        event_name="Purchase",
        event_id="pi_x",
        event_time=1,
        user_data=CapiUserData(email="jan@x.pl"),
    )
    assert ok is False


@respx.mock
async def test_send_event_api_error_returns_false(capi_enabled):
    respx.post(EVENTS_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad"}})
    )
    ok = await send_event(
        event_name="Purchase",
        event_id="pi_err",
        event_time=1,
        user_data=CapiUserData(email="jan@x.pl"),
    )
    assert ok is False


@respx.mock
async def test_send_event_network_error_returns_false(capi_enabled):
    respx.post(EVENTS_URL).mock(side_effect=httpx.ConnectError("boom"))
    ok = await send_event(
        event_name="Purchase",
        event_id="pi_net",
        event_time=1,
        user_data=CapiUserData(email="jan@x.pl"),
    )
    assert ok is False
