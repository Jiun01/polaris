"""Off-Meta channels M1 — unit tests for the Telegram branch of reply_dispatch.

DB-free; mocks the TelegramClient. Covers:
  - text-only send → send_message with body clipped to 4096
  - single image → send_photo carries the reply text as caption (clipped 1024)
  - multi-image → first photo captioned, rest bare; returns first message_id
  - missing client / token → returns None (no raise; row already committed)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from polaris_api.services.reply_dispatch import (
    ConversationRef,
    DispatchSkipped,
    send_reply,
)
from polaris_shared_types import ResponseEnvelope, UsageMetrics


def _envelope(content: str) -> ResponseEnvelope:
    return ResponseEnvelope(
        content=content,
        intent="simple",
        model_used="haiku",
        language_detected="en",
        usage=UsageMetrics(input_tokens=1, output_tokens=1),
        latency_ms=10,
    )


def _conv() -> ConversationRef:
    return ConversationRef(channel="telegram", channel_user_id="4242")


@pytest.mark.asyncio
async def test_text_only_calls_send_message_clipped() -> None:
    tg = MagicMock()
    tg.send_message = AsyncMock(return_value="555")
    out = await send_reply(
        conversation=_conv(),
        response=_envelope("z" * 4500),
        meta_client=MagicMock(),
        telegram_client=tg,
        telegram_token="bot-token",
    )
    assert out == "555"
    tg.send_message.assert_awaited_once()
    body = tg.send_message.await_args.kwargs["text"]
    assert len(body) <= 4096
    assert body.endswith("[truncated]")
    assert tg.send_message.await_args.kwargs["token"] == "bot-token"
    assert tg.send_message.await_args.kwargs["chat_id"] == "4242"


@pytest.mark.asyncio
async def test_single_image_carries_caption() -> None:
    tg = MagicMock()
    tg.send_photo = AsyncMock(return_value="777")
    out = await send_reply(
        conversation=_conv(),
        response=_envelope("look at this"),
        meta_client=MagicMock(),
        telegram_client=tg,
        telegram_token="bot-token",
        image_urls=["https://signed/img1.png"],
    )
    assert out == "777"
    tg.send_photo.assert_awaited_once()
    kwargs = tg.send_photo.await_args.kwargs
    assert kwargs["photo_url"] == "https://signed/img1.png"
    assert kwargs["caption"] == "look at this"


@pytest.mark.asyncio
async def test_caption_clipped_to_1024() -> None:
    tg = MagicMock()
    tg.send_photo = AsyncMock(return_value="1")
    await send_reply(
        conversation=_conv(),
        response=_envelope("c" * 2000),
        meta_client=MagicMock(),
        telegram_client=tg,
        telegram_token="bot-token",
        image_urls=["https://signed/img1.png"],
    )
    caption = tg.send_photo.await_args.kwargs["caption"]
    assert len(caption) <= 1024
    assert caption.endswith("[truncated]")


@pytest.mark.asyncio
async def test_multi_image_first_captioned_rest_bare() -> None:
    tg = MagicMock()
    tg.send_photo = AsyncMock(side_effect=["first", "second", "third"])
    out = await send_reply(
        conversation=_conv(),
        response=_envelope("gallery"),
        meta_client=MagicMock(),
        telegram_client=tg,
        telegram_token="bot-token",
        image_urls=["u1", "u2", "u3"],
    )
    assert out == "first"  # returns the first message_id
    assert tg.send_photo.await_count == 3
    first_call, second_call = tg.send_photo.await_args_list[0], tg.send_photo.await_args_list[1]
    assert first_call.kwargs["caption"] == "gallery"
    assert "caption" not in second_call.kwargs  # extras are bare


@pytest.mark.asyncio
async def test_missing_client_or_token_raises() -> None:
    """A missing client or bot token is a hard misconfig — DispatchSkipped, not
    a silent None drop (sub-fix D). The caller records it at ERROR + metric."""
    # No client wired.
    with pytest.raises(DispatchSkipped) as exc_info:
        await send_reply(
            conversation=_conv(),
            response=_envelope("hi"),
            meta_client=MagicMock(),
            telegram_client=None,
            telegram_token="bot-token",
        )
    assert exc_info.value.channel == "telegram"
    assert exc_info.value.reason == "no_client"

    # Client present but no token.
    tg = MagicMock()
    tg.send_message = AsyncMock()
    with pytest.raises(DispatchSkipped) as exc_info2:
        await send_reply(
            conversation=_conv(),
            response=_envelope("hi"),
            meta_client=MagicMock(),
            telegram_client=tg,
            telegram_token=None,
        )
    assert exc_info2.value.reason == "no_bot_token"
    tg.send_message.assert_not_awaited()
