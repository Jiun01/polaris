"""Tests for ``InstagramLoginClient`` — Instagram-Login DM send (migration 104).

Pins the graph.instagram.com ``/me/messages`` payload shape (recipient +
message, and crucially NO ``messaging_type``), the IG-Login token passed as the
``access_token`` query param (not a Bearer header), mid extraction, and
error/precondition handling. Self-contained httpx stub (mirrors
``test_meta_messaging.py``).
"""

from __future__ import annotations

import types
from typing import Any

import pytest
from polaris_api.services import meta_messaging
from polaris_api.services.meta_messaging import (
    InstagramLoginClient,
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
def ig_client() -> InstagramLoginClient:
    return InstagramLoginClient(
        base_url="https://graph.instagram.com", graph_api_version="v25.0"
    )


@pytest.mark.asyncio
async def test_send_text_payload_and_mid(
    ig_client: InstagramLoginClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch(monkeypatch, 200, {"recipient_id": "r", "message_id": "mid.T"})
    mid = await ig_client.send_text(
        access_token="IGLOGINTOK",
        recipient_id="IGSID_123",
        text="Sure, here you go!",
    )
    assert mid == "mid.T"
    sent = calls[-1]
    # Token is a query param, NOT an Authorization: Bearer header.
    assert sent["params"] == {"access_token": "IGLOGINTOK"}
    assert sent["json"]["recipient"] == {"id": "IGSID_123"}
    assert sent["json"]["message"] == {"text": "Sure, here you go!"}
    # The IG-Login messaging API rejects messaging_type — it must be absent.
    assert "messaging_type" not in sent["json"]
    # Bot reply (default) — no HUMAN_AGENT tag.
    assert "tag" not in sent["json"]
    # Sends via graph.instagram.com, not graph.facebook.com.
    assert sent["url"] == "https://graph.instagram.com/v25.0/me/messages"


@pytest.mark.asyncio
async def test_send_text_human_agent_tag(
    ig_client: InstagramLoginClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Human-agent IG-Login send → bare top-level tag=HUMAN_AGENT and STILL no
    messaging_type (graph.instagram.com rejects the Messenger-only field).
    Extends the window to 7 days for human-agent replies."""
    calls = _patch(monkeypatch, 200, {"message_id": "mid.HA"})
    mid = await ig_client.send_text(
        access_token="IGLOGINTOK",
        recipient_id="IGSID_5",
        text="Hi, this is Jane from support — following up.",
        human_agent=True,
    )
    assert mid == "mid.HA"
    sent = calls[-1]
    assert sent["json"]["tag"] == "HUMAN_AGENT"
    # Crucial divergence from the Messenger edge: NO messaging_type even when tagged.
    assert "messaging_type" not in sent["json"]


@pytest.mark.asyncio
async def test_send_image_human_agent_tag(
    ig_client: InstagramLoginClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch(monkeypatch, 200, {"message_id": "mid.HAI"})
    await ig_client.send_image(
        access_token="IGLOGINTOK",
        recipient_id="IGSID_5",
        image_url="https://signed.example/r.jpg",
        human_agent=True,
    )
    sent = calls[-1]
    assert sent["json"]["tag"] == "HUMAN_AGENT"
    assert "messaging_type" not in sent["json"]


@pytest.mark.asyncio
async def test_send_image_payload_and_mid(
    ig_client: InstagramLoginClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch(monkeypatch, 200, {"message_id": "mid.I"})
    mid = await ig_client.send_image(
        access_token="IGLOGINTOK",
        recipient_id="IGSID_9",
        image_url="https://signed.example/p.jpg",
    )
    assert mid == "mid.I"
    msg = calls[-1]["json"]["message"]
    assert msg["attachment"]["type"] == "image"
    assert msg["attachment"]["payload"]["url"] == "https://signed.example/p.jpg"
    assert "messaging_type" not in calls[-1]["json"]


@pytest.mark.asyncio
async def test_placeholder_token_raises(
    ig_client: InstagramLoginClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, 200, {"message_id": "x"})
    with pytest.raises(RuntimeError):
        await ig_client.send_text(
            access_token="placeholder-token",
            recipient_id="r",
            text="hi",
        )


@pytest.mark.asyncio
async def test_4xx_raises_messaging_error(
    ig_client: InstagramLoginClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, 401, {"error": {"message": "token revoked"}})
    with pytest.raises(MetaMessagingError) as exc:
        await ig_client.send_text(
            access_token="IGLOGINTOK",
            recipient_id="r",
            text="hi",
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_missing_message_id_raises(
    ig_client: InstagramLoginClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, 200, {"recipient_id": "r"})  # no message_id
    with pytest.raises(MetaMessagingError):
        await ig_client.send_image(
            access_token="IGLOGINTOK",
            recipient_id="r",
            image_url="https://x/y.jpg",
        )
