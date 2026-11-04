"""Tests for ``meta_messaging`` — IG/Messenger conversational send (Phase 15 PR-5).

Pins the Graph ``/me/messages`` payload shape (recipient + messaging_type +
message), the Page-token query param, mid extraction, and error/precondition
handling. Self-contained httpx stub (the client uses ``params=``, which the
shared Phase-6 stub doesn't model).
"""

from __future__ import annotations

import types
from typing import Any

import pytest
from polaris_api.services import meta_messaging
from polaris_api.services.meta_messaging import (
    MetaMessagingClient,
    MetaMessagingError,
)


class _FakeResp:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeClient:
    def __init__(self, resp: _FakeResp, calls: list[dict[str, Any]]) -> None:
        self._resp = resp
        self._calls = calls

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> _FakeResp:
        self._calls.append({"url": url, "params": params, "json": json})
        return self._resp


def _patch(
    monkeypatch: pytest.MonkeyPatch, status_code: int, body: dict[str, Any]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    resp = _FakeResp(status_code, body)

    def _factory(*_a: object, **_k: object) -> _FakeClient:
        return _FakeClient(resp, calls)

    monkeypatch.setattr(
        meta_messaging, "httpx", types.SimpleNamespace(AsyncClient=_factory)
    )
    return calls


@pytest.fixture
def msg_client() -> MetaMessagingClient:
    return MetaMessagingClient(
        base_url="https://graph.facebook.com", graph_api_version="v25.0"
    )


@pytest.mark.asyncio
async def test_send_text_payload_and_mid(
    msg_client: MetaMessagingClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch(monkeypatch, 200, {"recipient_id": "r", "message_id": "mid.T"})
    mid = await msg_client.send_text(
        page_access_token="PAGETOK",
        recipient_id="IGSID_123",
        text="We have it in blue!",
        platform="messenger",
    )
    assert mid == "mid.T"
    sent = calls[-1]
    assert sent["params"] == {"access_token": "PAGETOK"}
    assert sent["json"]["recipient"] == {"id": "IGSID_123"}
    assert sent["json"]["messaging_type"] == "RESPONSE"
    assert sent["json"]["message"] == {"text": "We have it in blue!"}
    # Bot reply (default) — no HUMAN_AGENT tag.
    assert "tag" not in sent["json"]
    assert sent["url"].endswith("/v25.0/me/messages")


@pytest.mark.asyncio
async def test_send_text_human_agent_tag(
    msg_client: MetaMessagingClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Human-agent send → messaging_type=MESSAGE_TAG + tag=HUMAN_AGENT
    (extends the messaging window to 7 days for human-agent replies)."""
    calls = _patch(monkeypatch, 200, {"message_id": "mid.HA"})
    mid = await msg_client.send_text(
        page_access_token="PAGETOK",
        recipient_id="PSID_5",
        text="Sorry for the wait — here's your refund status.",
        platform="messenger",
        human_agent=True,
    )
    assert mid == "mid.HA"
    sent = calls[-1]
    assert sent["json"]["messaging_type"] == "MESSAGE_TAG"
    assert sent["json"]["tag"] == "HUMAN_AGENT"


@pytest.mark.asyncio
async def test_send_image_human_agent_tag(
    msg_client: MetaMessagingClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch(monkeypatch, 200, {"message_id": "mid.HAI"})
    await msg_client.send_image(
        page_access_token="PAGETOK",
        recipient_id="PSID_5",
        image_url="https://signed.example/r.jpg",
        platform="messenger",
        human_agent=True,
    )
    sent = calls[-1]
    assert sent["json"]["messaging_type"] == "MESSAGE_TAG"
    assert sent["json"]["tag"] == "HUMAN_AGENT"


@pytest.mark.asyncio
async def test_send_image_payload_and_mid(
    msg_client: MetaMessagingClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch(monkeypatch, 200, {"message_id": "mid.I"})
    mid = await msg_client.send_image(
        page_access_token="PAGETOK",
        recipient_id="PSID_9",
        image_url="https://signed.example/p.jpg",
        platform="messenger",
    )
    assert mid == "mid.I"
    msg = calls[-1]["json"]["message"]
    assert msg["attachment"]["type"] == "image"
    assert msg["attachment"]["payload"]["url"] == "https://signed.example/p.jpg"


@pytest.mark.asyncio
async def test_placeholder_token_raises(
    msg_client: MetaMessagingClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, 200, {"message_id": "x"})
    with pytest.raises(RuntimeError):
        await msg_client.send_text(
            page_access_token="placeholder-token",
            recipient_id="r",
            text="hi",
            platform="messenger",
        )


@pytest.mark.asyncio
async def test_4xx_raises_messaging_error(
    msg_client: MetaMessagingClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, 401, {"error": {"message": "token revoked"}})
    with pytest.raises(MetaMessagingError) as exc:
        await msg_client.send_text(
            page_access_token="PAGETOK",
            recipient_id="r",
            text="hi",
            platform="messenger",
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_4xx_error_logs_structured_meta_fields_not_body(
    msg_client: MetaMessagingClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-2xx logs Meta's structured error fields (code/subcode/type/fbtrace)
    so an operator can tell e.g. code=190 (expired Page token) — but never the
    full body (which can echo token fragments via error_data). The raised
    exception message surfaces the code/subcode too."""
    body = {
        "error": {
            "message": "Error validating access token: Session has expired.",
            "type": "OAuthException",
            "code": 190,
            "error_subcode": 463,
            "error_data": {"sensitive": "PAGETOK-fragment-should-not-be-logged"},
            "fbtrace_id": "Abc123",
        }
    }
    _patch(monkeypatch, 400, body)
    with caplog.at_level("WARNING"), pytest.raises(MetaMessagingError) as exc:
        await msg_client.send_text(
            page_access_token="PAGETOK",
            recipient_id="r",
            text="hi",
            platform="messenger",
        )
    assert exc.value.status_code == 400
    assert "code=190" in str(exc.value)
    assert "subcode=463" in str(exc.value)
    log = caplog.text
    assert "code=190" in log and "subcode=463" in log
    assert "type=OAuthException" in log and "fbtrace=Abc123" in log
    assert "Session has expired" in log  # the human-readable message is kept
    # The raw body / error_data must NOT be dumped wholesale.
    assert "error_data" not in log
    assert "should-not-be-logged" not in log


@pytest.mark.asyncio
async def test_missing_message_id_raises(
    msg_client: MetaMessagingClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, 200, {"recipient_id": "r"})  # no message_id
    with pytest.raises(MetaMessagingError):
        await msg_client.send_image(
            page_access_token="PAGETOK",
            recipient_id="r",
            image_url="https://x/y.jpg",
            platform="messenger",
        )
