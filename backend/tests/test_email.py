import json

import httpx
import pytest
import respx

from app.core.config import settings
from app.core.email import (
    DEFAULT_FROM,
    EmailConfigError,
    EmailSendError,
    normalize_email,
    send_email,
)

RESEND_URL = "https://api.resend.com/emails"


@pytest.fixture
def resend_configured(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")


def test_normalize_email():
    assert normalize_email("  Jan.Kowalski@X.PL ") == "jan.kowalski@x.pl"


@respx.mock
async def test_send_email_success(resend_configured):
    route = respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "msg_1"}))

    msg_id = await send_email(
        to="jan@x.pl",
        subject="Potwierdź zapis",
        html="<b>hej</b>",
        reply_to="krystian@befreeclub.pl",
        headers={"X-Entity-Ref-ID": "uuid-1"},
    )

    assert msg_id == "msg_1"
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer re_test_key"
    body = json.loads(request.content)
    assert body["from"] == DEFAULT_FROM
    assert body["to"] == ["jan@x.pl"]
    assert body["subject"] == "Potwierdź zapis"
    assert body["html"] == "<b>hej</b>"
    assert body["reply_to"] == "krystian@befreeclub.pl"
    assert body["headers"] == {"X-Entity-Ref-ID": "uuid-1"}


@respx.mock
async def test_send_email_custom_from_no_optional(resend_configured):
    route = respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "msg_2"}))

    await send_email(
        to=["a@x.pl", "b@x.pl"],
        subject="x",
        html="y",
        from_email="Be Free Club <krystian@befreeclub.pl>",
    )

    body = json.loads(route.calls.last.request.content)
    assert body["from"] == "Be Free Club <krystian@befreeclub.pl>"
    assert body["to"] == ["a@x.pl", "b@x.pl"]
    assert "reply_to" not in body
    assert "headers" not in body


async def test_send_email_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", None)
    with pytest.raises(EmailConfigError):
        await send_email(to="jan@x.pl", subject="x", html="y")


@respx.mock
async def test_send_email_api_error(resend_configured):
    respx.post(RESEND_URL).mock(
        return_value=httpx.Response(422, json={"message": "invalid from"})
    )
    with pytest.raises(EmailSendError) as exc_info:
        await send_email(to="jan@x.pl", subject="x", html="y")
    assert exc_info.value.status == 422
    assert "invalid from" in (exc_info.value.body or "")


@respx.mock
async def test_send_email_network_error(resend_configured):
    respx.post(RESEND_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(EmailSendError) as exc_info:
        await send_email(to="jan@x.pl", subject="x", html="y")
    assert exc_info.value.status is None
