"""Klient Circle Admin API v2 (members): retry zaproszen, remove, paginacja."""

import json

import httpx
import pytest
import respx

from app.core.config import settings
from app.modules.members.services import circle

MEMBERS_URL = "https://app.circle.so/api/admin/v2/community_members"


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    async def _noop(_seconds):
        return None

    monkeypatch.setattr(circle, "_sleep", _noop)


@pytest.fixture
def circle_configured(monkeypatch):
    monkeypatch.setattr(settings, "CIRCLE_API_TOKEN", "circle_test_token")
    monkeypatch.setattr(settings, "CIRCLE_COMMUNITY_ID", "123")


@respx.mock
async def test_invite_success(circle_configured):
    route = respx.post(MEMBERS_URL).mock(
        return_value=httpx.Response(200, json={"id": 777})
    )

    result = await circle.invite("jan@x.pl")

    assert result.ok is True
    assert result.circle_member_id == "777"
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer circle_test_token"
    body = json.loads(request.content)
    assert body == {"community_id": 123, "email": "jan@x.pl", "skip_invitation": False}


@respx.mock
async def test_invite_community_member_id_fallback(circle_configured):
    respx.post(MEMBERS_URL).mock(
        return_value=httpx.Response(200, json={"community_member": {"id": 42}})
    )
    result = await circle.invite("jan@x.pl", skip_invitation=True)
    assert result.ok is True
    assert result.circle_member_id == "42"


@respx.mock
async def test_invite_4xx_no_retry(circle_configured):
    route = respx.post(MEMBERS_URL).mock(
        return_value=httpx.Response(422, text="already a member")
    )
    result = await circle.invite("jan@x.pl")
    assert result.ok is False
    assert route.call_count == 1  # 4xx (poza 429) bez ponowienia
    assert "422" in result.detail


@respx.mock
async def test_invite_retries_on_5xx(circle_configured):
    route = respx.post(MEMBERS_URL).mock(
        side_effect=[
            httpx.Response(500, text="boom"),
            httpx.Response(502, text="boom"),
            httpx.Response(200, json={"id": 9}),
        ]
    )
    result = await circle.invite("jan@x.pl")
    assert result.ok is True
    assert result.circle_member_id == "9"
    assert route.call_count == 3


@respx.mock
async def test_invite_retries_on_429(circle_configured):
    route = respx.post(MEMBERS_URL).mock(
        side_effect=[httpx.Response(429, text="slow down"), httpx.Response(200, json={"id": 1})]
    )
    result = await circle.invite("jan@x.pl")
    assert result.ok is True
    assert route.call_count == 2


@respx.mock
async def test_invite_gives_up_after_3_attempts(circle_configured):
    route = respx.post(MEMBERS_URL).mock(return_value=httpx.Response(500, text="boom"))
    result = await circle.invite("jan@x.pl")
    assert result.ok is False
    assert route.call_count == 3


@respx.mock
async def test_invite_retries_network_errors(circle_configured):
    route = respx.post(MEMBERS_URL).mock(
        side_effect=[httpx.ConnectError("net down"), httpx.Response(200, json={"id": 5})]
    )
    result = await circle.invite("jan@x.pl")
    assert result.ok is True
    assert route.call_count == 2


async def test_invite_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "CIRCLE_API_TOKEN", None)
    monkeypatch.setattr(settings, "CIRCLE_COMMUNITY_ID", None)
    result = await circle.invite("jan@x.pl")
    assert result.ok is False
    assert result.detail == "Circle credentials not configured"


@respx.mock
async def test_remove_success_and_404_treated_as_removed(circle_configured):
    route = respx.delete(url__startswith=MEMBERS_URL).mock(
        side_effect=[httpx.Response(200, json={}), httpx.Response(404, text="not found")]
    )
    assert await circle.remove("777") is True
    assert await circle.remove("777") is True
    assert "community_id=123" in str(route.calls.last.request.url)


@respx.mock
async def test_remove_failure(circle_configured):
    respx.delete(url__startswith=MEMBERS_URL).mock(return_value=httpx.Response(500, text="boom"))
    assert await circle.remove("777") is False


@respx.mock
async def test_fetch_all_members_paginates(circle_configured):
    route = respx.get(url__startswith=MEMBERS_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "records": [{"id": 1, "email": "A@x.pl"}, {"id": 2, "email": "b@x.pl"}],
                    "has_next_page": True,
                },
            ),
            httpx.Response(
                200,
                json={"records": [{"id": 3, "email": "c@x.pl"}], "has_next_page": False},
            ),
        ]
    )

    mapping = await circle.fetch_all_members()

    assert mapping == {"a@x.pl": "1", "b@x.pl": "2", "c@x.pl": "3"}
    assert route.call_count == 2
    first_url = str(route.calls[0].request.url)
    assert "per_page=50" in first_url and "page=1" in first_url
    assert "page=2" in str(route.calls[1].request.url)


@respx.mock
async def test_find_member_id_by_email(circle_configured):
    respx.get(url__startswith=MEMBERS_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"records": [{"id": 1, "email": "a@x.pl"}], "has_next_page": True},
            ),
            httpx.Response(
                200,
                json={"records": [{"id": 2, "email": "Jan@X.pl"}], "has_next_page": False},
            ),
        ]
    )
    assert await circle.find_member_id_by_email("jan@x.pl") == "2"


@respx.mock
async def test_find_member_id_by_email_not_found(circle_configured):
    respx.get(url__startswith=MEMBERS_URL).mock(
        return_value=httpx.Response(200, json={"records": [], "has_next_page": False})
    )
    assert await circle.find_member_id_by_email("ghost@x.pl") is None
