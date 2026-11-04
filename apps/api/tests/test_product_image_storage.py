"""Phase 15 PR-3 — product image processing (downscale + validate) unit tests.

The Supabase upload + signed-URL helpers are network-bound (httpx → Storage)
and covered by integration tests; this file unit-tests the pure, CPU-bound
``process_image`` pipeline — the security-relevant part (magic-byte validation
rejects non-images even when the client lies about Content-Type) plus the
downscale/re-encode contract.
"""

from __future__ import annotations

import io

import pytest
from polaris_api.services.products.storage import (
    ALLOWED_IMAGE_CONTENT_TYPES,
    ImageValidationError,
    process_image,
)


def _png_bytes(width: int, height: int) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (width, height), (200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_process_image_downscales_large_to_max_edge() -> None:
    out = process_image(_png_bytes(3000, 2000))
    # Longest edge clamped to 1600, aspect (landscape) preserved.
    assert max(out.width, out.height) == 1600
    assert out.width > out.height
    assert out.content_type == "image/jpeg"
    assert len(out.data) > 0


def test_process_image_only_shrinks_small_images_keep_dims() -> None:
    out = process_image(_png_bytes(800, 600))
    assert out.width == 800
    assert out.height == 600


def test_process_image_reencodes_png_to_jpeg() -> None:
    out = process_image(_png_bytes(1000, 1000))
    assert out.content_type == "image/jpeg"
    # JPEG SOI magic bytes.
    assert out.data[:2] == b"\xff\xd8"


def test_process_image_rejects_non_image() -> None:
    with pytest.raises(ImageValidationError):
        process_image(b"this is definitely not an image")


def test_allowed_content_types_are_the_decodable_set() -> None:
    assert ALLOWED_IMAGE_CONTENT_TYPES == frozenset(
        {"image/jpeg", "image/png", "image/webp"}
    )
