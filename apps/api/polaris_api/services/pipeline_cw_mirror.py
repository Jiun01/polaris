"""Chatwoot reply-mirroring + product-image-note posting — extracted from
pipeline.py (Batch E, task E1; see PRP plan audit-batch-e-structural-splits).

``_mirror_reply_to_cw`` (+ its ``_format_product_image_note`` /
``_post_image_note`` helpers) mirrors a dispatched bot reply into the linked
Chatwoot conversation; ``_mint_product_image_urls`` mints short-TTL signed
read URLs for product images at send time. Called from ``pipeline._run_inner``
(3x) and ``pipeline._dispatch_reply`` (1x) respectively.

The LEAST coupled of the 3 E1 extractions — no call-backs into pipeline.py's
own helpers, only ``get_settings``, ``metrics``, and late-imported
``chatwoot.client`` / ``products.storage`` (unchanged from pipeline.py).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from polaris_shared_types import ResponseEnvelope

from polaris_api import metrics
from polaris_api.config import get_settings

logger = logging.getLogger(__name__)

_IMAGE_NOTE_MAX_LEN = 2000


def _format_product_image_note(images: list[Any]) -> str | None:
    """Build the agent-facing note summarising product images the bot sent.

    Product images go DIRECT to the customer via Meta (they are not mirrored
    into Chatwoot as attachments). This note is posted as a Chatwoot private
    note so a human agent who takes over sees what the bot already sent.

    Images are grouped by ``product_name``; any without a name fall back to a
    generic count. Insertion order is preserved for deterministic output.
    Returns ``None`` when there are no images (caller skips the post).
    """
    if not images:
        return None
    named: dict[str, int] = {}
    unnamed = 0
    for img in images:
        name = (getattr(img, "product_name", None) or "").strip()
        if name:
            named[name] = named.get(name, 0) + 1
        else:
            unnamed += 1
    lines = [
        f"{count} image{'s' if count != 1 else ''} sent for {name}" for name, count in named.items()
    ]
    if unnamed:
        lines.append(f"{unnamed} image{'s' if unnamed != 1 else ''} sent")
    if not lines:
        return None
    note = "\n".join(lines)
    if len(note) > _IMAGE_NOTE_MAX_LEN:
        note = note[: _IMAGE_NOTE_MAX_LEN - 1] + "…"
    return note


async def _post_image_note(
    cw_client: Any,
    *,
    account_id: int,
    conversation_id: int,
    images: list[Any],
) -> None:
    """Best-effort: post the product-image summary as a Chatwoot private note.

    No-ops when the reply carried no images. A failure here never affects the
    customer (the image + text were already dispatched) — it is logged and
    swallowed, matching the surrounding mirror's best-effort posture.
    """
    note = _format_product_image_note(images)
    if note is None:
        return
    try:
        await cw_client.post_private_note(
            account_id=account_id,
            conversation_id=conversation_id,
            content=note,
        )
    except Exception:  # noqa: BLE001 — agent-facing annotation, never fatal
        logger.warning("pipeline: product-image note post failed cw_conv=%s", conversation_id)


async def _mirror_reply_to_cw(
    *,
    cw_account_id: int,
    cw_conv_id: int,
    response: ResponseEnvelope,
    provider_message_id: str | None,
    tenant_id: UUID,
) -> None:
    """Mirror a dispatched bot reply into the linked CW conversation (best-effort).

    Makes the reply visible to agents in the CW UI and satisfies
    ``pollCwBotReply()`` in the Phase 12 E2E specs. Posts the reply text plus a
    product-image summary note. The reply has ALREADY been dispatched to the
    provider, so every failure here is logged and swallowed — control flow is
    unchanged. A ``ChatwootConfigError`` (deploy misconfig, e.g.
    ``CHATWOOT_BOT_ACCESS_TOKEN`` unset) and a 401/403 auth rejection (stale
    token) are surfaced at ERROR; everything else is a routine WARNING.

    Callers guard on ``cw_account_id and cw_conv_id and response.content``.
    """
    try:
        from polaris_api.services.chatwoot.client import (
            ChatwootConfigError as _ChatwootConfigError,
        )
        from polaris_api.services.chatwoot.client import (
            ChatwootError as _ChatwootError,
        )
        from polaris_api.services.chatwoot.client import (
            get_chatwoot_client as _get_cw_client,
        )

        await _get_cw_client().post_outbound_message(
            account_id=cw_account_id,
            conversation_id=cw_conv_id,
            content=response.content,
            source_id=provider_message_id,
        )
        await _post_image_note(
            _get_cw_client(),
            account_id=cw_account_id,
            conversation_id=cw_conv_id,
            images=response.images,
        )
    except _ChatwootConfigError as exc:
        # Deploy misconfig (e.g. CHATWOOT_BOT_ACCESS_TOKEN unset) — not a
        # transient blip. ChatwootConfigError carries status_code=None so it
        # would otherwise be logged as a routine "HTTP None" warning below.
        # Surface at ERROR so the misconfig is visible. Reply already dispatched.
        logger.error(
            "pipeline: CW reply mirror not configured tenant=%s cw_conv=%s — %s",
            tenant_id,
            cw_conv_id,
            exc,
        )
    except _ChatwootError as exc:
        # A 401/403 here means CHATWOOT_BOT_ACCESS_TOKEN is stale/invalid — the
        # mirror silently fails for EVERY reply (replies reach the customer but
        # never show in the CW inbox). Surface that loudly rather than as a
        # routine warning. Control flow is unchanged — the reply already went out.
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            logger.error(
                "pipeline: CW reply mirror auth-rejected (HTTP %s) tenant=%s "
                "cw_conv=%s — CHATWOOT_BOT_ACCESS_TOKEN invalid; re-sync from "
                "AgentBot.find(1).access_token.token. Reply already dispatched.",
                status,
                tenant_id,
                cw_conv_id,
            )
        else:
            logger.warning(
                "pipeline: CW reply mirroring failed (HTTP %s) tenant=%s "
                "cw_conv=%s — reply already dispatched to provider",
                status,
                tenant_id,
                cw_conv_id,
            )
    except Exception:
        logger.warning(
            "pipeline: CW reply mirroring failed "
            "tenant=%s cw_conv=%s — reply already dispatched to provider",
            tenant_id,
            cw_conv_id,
        )


async def _mint_product_image_urls(images: list[Any], *, tenant_id: UUID) -> list[str]:
    """Mint short-TTL signed read URLs for product images at send time.

    One bad image never kills the reply — a per-image mint failure is skipped
    and the text still sends. But drops are no longer silent: each drop bumps
    the ``polaris_outbound_image_mint_drops`` metric, and a TOTAL drop (an
    image-centric reply where EVERY image was lost, so the customer sees text
    with none of the promised images) is logged at ERROR with ``scope='total'``
    so ops can catch a broken storage path. ``images`` are ``OutboundImage``
    rows.

    Returns [] when storage is unconfigured — for a reply that DID carry images
    that is itself a total drop (deploy/config issue), so it is surfaced the
    same way rather than passing as a benign text-only reply.
    """
    from polaris_api.services.products import storage as product_storage

    total = len(images)
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_key:
        logger.error(
            "pipeline: %d product image(s) present but storage unconfigured — "
            "ALL dropped, sending text only (tenant=%s)",
            total,
            tenant_id,
        )
        metrics.record_image_mint_drops(tenant_id=tenant_id, dropped=total, scope="total")
        return []
    urls: list[str] = []
    for img in images:
        try:
            urls.append(await product_storage.signed_read_url(img.storage_path, settings))
        except Exception:  # noqa: BLE001 — skip the one image, keep the reply
            logger.warning(
                "pipeline: failed to mint signed URL for a product image — skipping it (tenant=%s)",
                tenant_id,
            )
    dropped = total - len(urls)
    if dropped:
        if not urls:
            # Image-centric reply lost EVERY image — the customer gets bare text
            # where images were promised. Permanent-looking; surface loudly.
            logger.error(
                "pipeline: ALL %d product image(s) dropped at send — text-only "
                "reply sent without promised images (tenant=%s)",
                total,
                tenant_id,
            )
            metrics.record_image_mint_drops(tenant_id=tenant_id, dropped=dropped, scope="total")
        else:
            metrics.record_image_mint_drops(tenant_id=tenant_id, dropped=dropped, scope="partial")
    return urls
