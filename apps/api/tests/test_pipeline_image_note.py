"""Product-image agent-note tests (Chatwoot-native rehaul, Step 6).

Product images go DIRECT to the customer via Meta; they are NOT mirrored into
Chatwoot as attachments. Instead the pipeline posts an agent-facing private note
("N images sent for {product}") so a human who takes over sees what the bot sent.

Covers the pure formatter ``_format_product_image_note`` and the best-effort
poster ``_post_image_note`` (asyncio_mode=auto — no marker needed).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from polaris_api.services.pipeline_cw_mirror import (
    _format_product_image_note,
    _post_image_note,
)
from polaris_shared_types.llm_response import OutboundImage


def _img(name: str | None = None) -> OutboundImage:
    return OutboundImage(storage_path="products/img.jpg", product_name=name)


# --- _format_product_image_note ------------------------------------------------


def test_format_returns_none_for_empty() -> None:
    assert _format_product_image_note([]) is None


def test_format_single_named_is_singular() -> None:
    assert _format_product_image_note([_img("Widget")]) == "1 image sent for Widget"


def test_format_two_of_same_product_pluralises() -> None:
    note = _format_product_image_note([_img("Widget"), _img("Widget")])
    assert note == "2 images sent for Widget"


def test_format_multiple_products_one_line_each_in_order() -> None:
    note = _format_product_image_note([_img("Alpha"), _img("Beta")])
    assert note == "1 image sent for Alpha\n1 image sent for Beta"


def test_format_unnamed_falls_back_to_generic_count() -> None:
    assert _format_product_image_note([_img(None)]) == "1 image sent"


def test_format_mixes_named_and_unnamed() -> None:
    note = _format_product_image_note([_img("Alpha"), _img(None), _img(None)])
    assert note == "1 image sent for Alpha\n2 images sent"


def test_format_treats_blank_name_as_unnamed() -> None:
    assert _format_product_image_note([_img("   ")]) == "1 image sent"


def test_format_caps_long_note_length() -> None:
    # Many distinct long names → note would exceed the cap; must be bounded.
    imgs = [_img(f"Product-{i}-{'x' * 50}") for i in range(60)]
    note = _format_product_image_note(imgs)
    assert note is not None
    assert len(note) <= 2000
    assert note.endswith("…")


# --- _post_image_note ----------------------------------------------------------


async def test_post_image_note_posts_private_note_when_images() -> None:
    client = AsyncMock()
    await _post_image_note(client, account_id=7, conversation_id=42, images=[_img("Widget")])
    client.post_private_note.assert_awaited_once_with(
        account_id=7, conversation_id=42, content="1 image sent for Widget"
    )


async def test_post_image_note_noop_when_no_images() -> None:
    client = AsyncMock()
    await _post_image_note(client, account_id=7, conversation_id=42, images=[])
    client.post_private_note.assert_not_awaited()


async def test_post_image_note_swallows_chatwoot_errors() -> None:
    client = AsyncMock()
    client.post_private_note.side_effect = RuntimeError("chatwoot down")
    # Must not raise — the image + text already reached the customer.
    await _post_image_note(client, account_id=7, conversation_id=42, images=[_img("Widget")])
    client.post_private_note.assert_awaited_once()
