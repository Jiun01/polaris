"""Tests for ``meta_whatsapp.send_image`` (Phase 15 PR-5).

WhatsApp natively supports an image caption, so the RAG answer rides on the
image (one message). Covers the payload shape, the caption omit-when-empty
path, and 4xx error classification.
"""

from __future__ import annotations

import pytest
from polaris_api.services.meta_whatsapp import (
    MetaWhatsAppClient,
    MetaWhatsAppError,
)

from .conftest import MetaResponse


@pytest.mark.asyncio
async def test_send_image_posts_image_payload_with_caption(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,  # noqa: ARG001 — fixture wires the patch
    meta_route_map: dict,
) -> None:
    meta_route_map[("POST", "DEMO_PID/messages")] = MetaResponse(
        status_code=200,
        json_body={"messages": [{"id": "wamid.img.001"}]},
    )

    wamid = await meta_client.send_image(
        phone_number_id="DEMO_PID",
        to="60123456789",
        image_link="https://signed.example/p.jpg",
        caption="Blue Tumbler — RM 49.90",
        access_token="EAA-token",
    )

    assert wamid == "wamid.img.001"
    call = stub_httpx_client.calls[-1]
    assert call.json["type"] == "image"
    assert call.json["image"]["link"] == "https://signed.example/p.jpg"
    assert call.json["image"]["caption"] == "Blue Tumbler — RM 49.90"


@pytest.mark.asyncio
async def test_send_image_without_caption_omits_caption_key(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,  # noqa: ARG001
    meta_route_map: dict,
) -> None:
    meta_route_map[("POST", "DEMO_PID/messages")] = MetaResponse(
        status_code=200,
        json_body={"messages": [{"id": "wamid.img.002"}]},
    )

    wamid = await meta_client.send_image(
        phone_number_id="DEMO_PID",
        to="60123456789",
        image_link="https://signed.example/p.jpg",
        access_token="EAA-token",
    )

    assert wamid == "wamid.img.002"
    assert "caption" not in stub_httpx_client.calls[-1].json["image"]


@pytest.mark.asyncio
async def test_send_image_4xx_raises_meta_error(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,  # noqa: ARG001
    meta_route_map: dict,
) -> None:
    meta_route_map[("POST", "DEMO_PID/messages")] = MetaResponse(
        status_code=400,
        json_body={"error": {"message": "bad image"}},
    )

    with pytest.raises(MetaWhatsAppError) as exc:
        await meta_client.send_image(
            phone_number_id="DEMO_PID",
            to="60123456789",
            image_link="https://signed.example/p.jpg",
            access_token="EAA-token",
        )
    assert exc.value.status_code == 400
