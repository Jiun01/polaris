"""Supabase Storage + image processing for the product catalog — Phase 15 PR-3.

Product photos pass THROUGH the API (multipart upload) rather than via a
client-direct signed-upload URL, because we validate + downscale server-side
(ADR-061 §6). A single resize per upload is cheap — unlike KB ingestion's bulk
OCR/embedding, which is why that stays on the desktop worker.

All Supabase calls use httpx + the service-role key (mirrors
``services/billing/storage.py``). The bucket is private; the bot fetches images
via a short-TTL signed read URL minted at send time (:func:`signed_read_url`).
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Final

import httpx

from polaris_api.config import Settings

logger = logging.getLogger(__name__)

PRODUCT_IMAGE_BUCKET: Final[str] = "polaris-product-images"

# Inbound content types accepted for product photos (everything is re-encoded
# to JPEG on the way in, so this is just the gate on what we'll try to decode).
ALLOWED_IMAGE_CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {"image/jpeg", "image/png", "image/webp"}
)

# Hard ceiling on the raw upload the handler reads before downscaling.
DEFAULT_MAX_UPLOAD_BYTES: Final[int] = 15 * 1024 * 1024  # 15 MB

# Downscale target: longest edge (px) + JPEG quality. Keeps photos well under
# Messenger's 8 MB URL limit and stretches the per-tenant MB cap ~5-10x.
_MAX_EDGE_PX: Final[int] = 1600
_JPEG_QUALITY: Final[int] = 80


@dataclass(slots=True, frozen=True)
class ProcessedImage:
    """A validated + downscaled image ready to persist."""

    data: bytes
    width: int
    height: int
    content_type: str


class ImageValidationError(ValueError):
    """Raised when an uploaded blob is not a decodable image."""


def process_image(raw: bytes) -> ProcessedImage:
    """Validate, downscale, and re-encode an uploaded image.

    * Magic-byte validation: Pillow refuses to open a non-image, surfaced as
      :class:`ImageValidationError` (so a renamed ``.exe`` is rejected even if
      the client lied about ``Content-Type``).
    * Downscale: longest edge clamped to ``_MAX_EDGE_PX`` preserving aspect;
      only ever shrinks.
    * Re-encode: always JPEG q=80, alpha/palette flattened to RGB so downstream
      send is uniform + small. (Transparency is rare for product photos.)

    Pillow is a declared dependency; the lazy import + fallback only matters in
    a stripped build, where we store the original bytes unmodified.
    """
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        logger.warning(
            "Pillow unavailable — storing product image without downscale"
        )
        return ProcessedImage(
            data=raw, width=0, height=0, content_type="image/jpeg"
        )

    try:
        with Image.open(io.BytesIO(raw)) as opened:
            oriented = ImageOps.exif_transpose(opened)  # honour camera orientation
            source = oriented if oriented is not None else opened
            rgb = source.convert("RGB")  # flatten alpha/palette → RGB for JPEG
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageValidationError("not a valid image file") from exc

    rgb.thumbnail((_MAX_EDGE_PX, _MAX_EDGE_PX))  # in-place, preserves aspect

    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return ProcessedImage(
        data=out.getvalue(),
        width=rgb.width,
        height=rgb.height,
        content_type="image/jpeg",
    )


async def upload_image(
    object_key: str, data: bytes, content_type: str, settings: Settings
) -> None:
    """Upload image bytes to the product-images bucket (service-role).

    Raises ``httpx.HTTPStatusError`` on a non-2xx response. Upload happens
    BEFORE the DB row is inserted so a storage failure leaves no orphan row.
    """
    url = (
        f"{settings.supabase_url}/storage/v1/object/"
        f"{PRODUCT_IMAGE_BUCKET}/{object_key}"
    )
    headers = {
        "Authorization": f"Bearer {settings.supabase_service_key}",
        "Content-Type": content_type,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, content=data, headers=headers)
        resp.raise_for_status()


async def delete_object(object_key: str, settings: Settings) -> None:
    """Delete a stored image object (service-role). Idempotent-ish: a 404 (the
    object was already gone) is treated as success. Raises
    ``httpx.HTTPStatusError`` on other non-2xx so the caller can log it.
    """
    url = (
        f"{settings.supabase_url}/storage/v1/object/"
        f"{PRODUCT_IMAGE_BUCKET}/{object_key}"
    )
    headers = {"Authorization": f"Bearer {settings.supabase_service_key}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.delete(url, headers=headers)
        if resp.status_code == 404:
            return
        resp.raise_for_status()


async def signed_read_url(
    object_key: str, settings: Settings, expires_in: int = 900
) -> str:
    """Return a short-TTL signed read URL for a stored image.

    Used at send time (PR-6) so Meta can fetch the image from the private
    bucket; 15-minute default TTL is ample for the fetch which happens within
    seconds. Raises ``httpx.HTTPStatusError`` on a non-2xx response.
    """
    url = (
        f"{settings.supabase_url}/storage/v1/object/sign/"
        f"{PRODUCT_IMAGE_BUCKET}/{object_key}"
    )
    headers = {"Authorization": f"Bearer {settings.supabase_service_key}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url, json={"expiresIn": expires_in}, headers=headers
        )
        resp.raise_for_status()
        data = resp.json()
        return f"{settings.supabase_url}/storage/v1{data['signedURL']}"
