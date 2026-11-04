"""Phase 15 PR-6 — product-image reply path unit tests.

Covers the two surviving pieces:
  * ``llm.select_product_skus`` — structured SKU selection (mocked Anthropic).
  * ``reply_dispatch.send_reply`` — per-channel text+image sequencing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from polaris_api.services import llm
from polaris_api.services.reply_dispatch import (
    ConversationRef,
    DispatchSkipped,
    send_reply,
)
from polaris_shared_types import (
    OutboundImage,
    ResponseEnvelope,
    UsageMetrics,
)


def _msg(text: str) -> MagicMock:
    m = MagicMock()
    block = MagicMock()
    block.text = text
    m.content = [block]
    return m


def _env(content: str = "answer", images: list[OutboundImage] | None = None) -> ResponseEnvelope:
    return ResponseEnvelope(
        content=content,
        intent="simple",
        model_used="haiku",
        language_detected="en",
        usage=UsageMetrics(input_tokens=1, output_tokens=1),
        latency_ms=1,
        images=images or [],
    )


# --------------------------------------------------------- select_product_skus


@pytest.mark.asyncio
async def test_select_returns_chosen_ids(mock_anthropic: MagicMock) -> None:
    mock_anthropic.messages.create.return_value = _msg('{"skus": ["a"]}')
    out = await llm.select_product_skus(
        "show me the blue one",
        [
            {"id": "a", "name": "Blue", "description": "d"},
            {"id": "b", "name": "Red", "description": "d"},
        ],
    )
    assert out == ["a"]


@pytest.mark.asyncio
async def test_select_filters_unknown_ids(mock_anthropic: MagicMock) -> None:
    mock_anthropic.messages.create.return_value = _msg('{"skus": ["a", "zzz"]}')
    out = await llm.select_product_skus(
        "x", [{"id": "a", "name": "A", "description": "d"}]
    )
    assert out == ["a"]


@pytest.mark.asyncio
async def test_select_empty_candidates_short_circuits() -> None:
    # No client call needed; returns [] before touching the LLM.
    assert await llm.select_product_skus("anything", []) == []


@pytest.mark.asyncio
async def test_select_invalid_json_is_failsafe(mock_anthropic: MagicMock) -> None:
    mock_anthropic.messages.create.return_value = _msg("sorry, no JSON here")
    out = await llm.select_product_skus(
        "x", [{"id": "a", "name": "A", "description": "d"}]
    )
    assert out == []


# --------------------------------------------------------- reply_dispatch


class _FakeMeta:
    def __init__(self) -> None:
        self.demo_phone_number_id = "DEMO"
        self.text_calls: list[tuple[str, str]] = []
        self.image_calls: list[tuple[str, str | None]] = []
        self.last_phone_number_id: str | None = None
        self.last_access_token: str | None = None

    async def send_text(
        self, *, phone_number_id: str, to: str, body: str,
        access_token: str | None = None,
    ) -> str:
        self.text_calls.append((to, body))
        self.last_phone_number_id = phone_number_id
        self.last_access_token = access_token
        return "wamid.text"

    async def send_image(
        self, *, phone_number_id: str, to: str, image_link: str,
        caption: str | None = None, access_token: str | None = None,
    ) -> str:
        self.image_calls.append((image_link, caption))
        self.last_phone_number_id = phone_number_id
        self.last_access_token = access_token
        return f"wamid.img{len(self.image_calls)}"


class _FakeMsg:
    def __init__(self) -> None:
        self.text_calls: list[tuple[str, str, str]] = []
        self.image_calls: list[tuple[str, str]] = []

    async def send_text(
        self, *, page_access_token: str, recipient_id: str, text: str, platform: str
    ) -> str:
        self.text_calls.append((recipient_id, text, platform))
        return "mid.text"

    async def send_image(
        self, *, page_access_token: str, recipient_id: str, image_url: str,
        platform: str,
    ) -> str:
        self.image_calls.append((image_url, platform))
        return "mid.img"


@pytest.mark.asyncio
async def test_whatsapp_text_only() -> None:
    meta = _FakeMeta()
    mid = await send_reply(
        conversation=ConversationRef(channel="whatsapp", channel_user_id="60123"),
        response=_env(),
        meta_client=meta,  # type: ignore[arg-type]
        whatsapp_phone_number_id="TENANT_PID",
        whatsapp_access_token="tenant-tok",
        image_urls=[],
    )
    assert mid == "wamid.text"
    assert meta.text_calls and not meta.image_calls


@pytest.mark.asyncio
async def test_whatsapp_images_caption_first_only() -> None:
    meta = _FakeMeta()
    mid = await send_reply(
        conversation=ConversationRef(channel="whatsapp", channel_user_id="60123"),
        response=_env("Blue tumbler"),
        meta_client=meta,  # type: ignore[arg-type]
        whatsapp_phone_number_id="TENANT_PID",
        whatsapp_access_token="tenant-tok",
        image_urls=["u1", "u2"],
    )
    assert mid == "wamid.img1"
    assert meta.image_calls == [("u1", "Blue tumbler"), ("u2", None)]
    assert not meta.text_calls  # caption carried the text


@pytest.mark.asyncio
async def test_whatsapp_per_tenant_credentials_routed() -> None:
    """When the caller supplies per-tenant WABA creds, the send uses the
    tenant's phone_number_id + token (not the demo number)."""
    meta = _FakeMeta()
    mid = await send_reply(
        conversation=ConversationRef(channel="whatsapp", channel_user_id="60123"),
        response=_env(),
        meta_client=meta,  # type: ignore[arg-type]
        whatsapp_phone_number_id="TENANT_PID",
        whatsapp_access_token="tenant-tok",
        image_urls=[],
    )
    assert mid == "wamid.text"
    assert meta.last_phone_number_id == "TENANT_PID"
    assert meta.last_access_token == "tenant-tok"


@pytest.mark.asyncio
async def test_whatsapp_missing_creds_raises_not_demo_fallback() -> None:
    """A real 'whatsapp' channel with unresolved per-tenant creds must NOT fall
    back to the shared demo number — it raises DispatchSkipped so the caller can
    record a permanent failure (sub-fix B)."""
    meta = _FakeMeta()
    with pytest.raises(DispatchSkipped) as exc_info:
        await send_reply(
            conversation=ConversationRef(channel="whatsapp", channel_user_id="60123"),
            response=_env(),
            meta_client=meta,  # type: ignore[arg-type]
            image_urls=[],
        )
    assert exc_info.value.channel == "whatsapp"
    assert exc_info.value.reason == "missing_credentials"
    # Nothing was sent — in particular NOT from the demo number.
    assert not meta.text_calls and not meta.image_calls


@pytest.mark.asyncio
async def test_messenger_text_then_image() -> None:
    meta, msg = _FakeMeta(), _FakeMsg()
    mid = await send_reply(
        conversation=ConversationRef(channel="messenger", channel_user_id="PSID"),
        response=_env("hi"),
        meta_client=meta,  # type: ignore[arg-type]
        messaging_client=msg,  # type: ignore[arg-type]
        page_access_token="TOK",
        image_urls=["u1"],
    )
    assert mid == "mid.text"
    assert msg.text_calls == [("PSID", "hi", "messenger")]
    assert msg.image_calls == [("u1", "messenger")]


@pytest.mark.asyncio
async def test_instagram_without_page_token_raises() -> None:
    """No Page token → DispatchSkipped (permanent misconfig the caller records),
    not a silent None drop."""
    meta, msg = _FakeMeta(), _FakeMsg()
    with pytest.raises(DispatchSkipped) as exc_info:
        await send_reply(
            conversation=ConversationRef(channel="messenger", channel_user_id="IGSID"),
            response=_env(),
            meta_client=meta,  # type: ignore[arg-type]
            messaging_client=msg,
            page_access_token=None,
            image_urls=[],
        )
    assert exc_info.value.channel == "messenger"
    assert exc_info.value.reason == "no_page_token"
    assert not msg.text_calls and not msg.image_calls


class _FakeIgLogin:
    """Stub InstagramLoginClient: no `platform` kwarg, no messaging_type."""

    def __init__(self) -> None:
        self.text_calls: list[tuple[str, str]] = []
        self.image_calls: list[str] = []

    async def send_text(
        self, *, access_token: str, recipient_id: str, text: str
    ) -> str:
        self.text_calls.append((recipient_id, text))
        return "mid.ig.text"

    async def send_image(
        self, *, access_token: str, recipient_id: str, image_url: str
    ) -> str:
        self.image_calls.append(image_url)
        return "mid.ig.img"


@pytest.mark.asyncio
async def test_instagram_login_text_then_image() -> None:
    """channel='instagram_login' routes to the IG-Login client with the
    IG-Login token (text first, then image — no native caption)."""
    meta, ig = _FakeMeta(), _FakeIgLogin()
    mid = await send_reply(
        conversation=ConversationRef(
            channel="instagram_login", channel_user_id="IGSID"
        ),
        response=_env("hi"),
        meta_client=meta,  # type: ignore[arg-type]
        instagram_login_client=ig,  # type: ignore[arg-type]
        ig_login_access_token="IGLOGINTOK",
        image_urls=["u1"],
    )
    assert mid == "mid.ig.text"
    assert ig.text_calls == [("IGSID", "hi")]
    assert ig.image_calls == ["u1"]


@pytest.mark.asyncio
async def test_instagram_login_without_token_raises() -> None:
    """No IG-Login token → DispatchSkipped, not a silent None drop."""
    meta, ig = _FakeMeta(), _FakeIgLogin()
    with pytest.raises(DispatchSkipped) as exc_info:
        await send_reply(
            conversation=ConversationRef(
                channel="instagram_login", channel_user_id="IGSID"
            ),
            response=_env(),
            meta_client=meta,  # type: ignore[arg-type]
            instagram_login_client=ig,  # type: ignore[arg-type]
            ig_login_access_token=None,
            image_urls=[],
        )
    assert exc_info.value.channel == "instagram_login"
    assert exc_info.value.reason == "no_ig_login_token"
    assert not ig.text_calls and not ig.image_calls
