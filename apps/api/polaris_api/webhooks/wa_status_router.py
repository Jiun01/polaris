"""POST /v1/webhooks/wa-status — Phase 6 PR-7.

Per-message delivery state from Meta's WhatsApp Cloud API v21.0.
Wire shape per the doc-lookup in 04g §1.2:

    {
      "object": "whatsapp_business_account",
      "entry": [
        {
          "id": "<waba_id>",
          "changes": [
            {
              "field": "messages",
              "value": {
                "statuses": [
                  {
                    "id": "wamid.XXXX",
                    "status": "sent" | "delivered" | "read" | "failed",
                    "timestamp": "1715512345",
                    "recipient_id": "60123456789",
                    "errors": [{...}]  // present on failed
                  },
                  ...
                ]
              }
            }
          ]
        }
      ]
    }

Each entry may carry N statuses; we iterate every one. Idempotency
on ``(wamid, status)`` so Meta retries short-circuit at the
webhook_events UNIQUE. The lifecycle dispatcher
(``services/broadcast/delivery.apply_status_update``) is itself
idempotent — running it twice is a no-op.

Behaviour matrix:
    missing/wrong sig   → 401 + hmac_fail forensic row
    valid sig, 0 events → 200 + non_message_event row
    valid sig, N events → 200 (dispatch each)
    DB outage           → 503 (Meta retries with backoff)
    JSON malformed      → 400
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response, status

from polaris_api.config import get_settings
from polaris_api.services.broadcast import delivery as broadcast_delivery
from polaris_api.services.webhook_resolver import record_event
from polaris_api.webhooks._hmac import verify_meta_signature

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

_META_SIGNATURE_HEADER = "x-hub-signature-256"


@router.post(
    "/wa-status",
    summary="Meta WhatsApp delivery status webhook",
    description=(
        "Receives per-message ``statuses[]`` events (sent / delivered "
        "/ read / failed) per Meta WhatsApp Cloud API v21.0. Resolves "
        "each wamid to a ``broadcast_recipients`` row via the service "
        "pool (cross-tenant lookup), then sets app.current_tenant_id "
        "explicitly before applying the forward-only state transition. "
        "Unknown wamids are no-ops (legitimate for non-broadcast sends "
        "or DSAR-cascaded rows). Idempotency via webhook_events UNIQUE "
        "on (source='wa-status', external_id=<wamid>:<status>)."
    ),
)
async def wa_status_inbound(request: Request) -> Response:
    raw: bytes = await request.body()
    header = request.headers.get(_META_SIGNATURE_HEADER)
    secret = get_settings().meta_app_secret
    payload_hash = hashlib.sha256(raw).digest()
    payload_hash_hex = payload_hash.hex()

    if not verify_meta_signature(raw, header, secret):
        logger.warning("wa-status webhook HMAC verify failed")
        try:
            await record_event(
                source="wa-status",
                external_id=payload_hash_hex,
                status="hmac_fail",
                payload_hash=payload_hash,
                error_class="HMACVerifyFailed",
            )
        except Exception as exc:  # noqa: BLE001 — see meta_template_status_router
            logger.error(
                "wa-status: record_event during hmac_fail failed: %s", exc
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning("wa-status: malformed JSON body: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc

    events_fired = 0
    try:
        for entry in body.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                if change.get("field") != "messages":
                    continue
                value = change.get("value") or {}
                if not isinstance(value, dict):
                    continue
                for sts in value.get("statuses", []) or []:
                    fired = await _dispatch_one(sts, payload_hash=payload_hash)
                    if fired:
                        events_fired += 1
    except asyncpg.PostgresError as exc:
        logger.error("wa-status: DB failure during dispatch: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE
        ) from exc

    if events_fired == 0:
        try:
            await record_event(
                source="wa-status",
                external_id=f"empty:{payload_hash_hex}:{datetime.now(UTC).timestamp()}",
                status="non_message_event",
                payload_hash=payload_hash,
            )
        except asyncpg.PostgresError as exc:
            logger.error(
                "wa-status: record_event during empty-body failed: %s", exc
            )

    return Response(status_code=status.HTTP_200_OK)


async def _dispatch_one(
    sts: dict[str, Any], *, payload_hash: bytes
) -> bool:
    """Apply one status event. Returns True iff dispatched.

    Idempotency key per 04g §6.2: ``<wamid>:<status>``. Meta sends
    timestamp too — we don't include it in the key because Meta
    can re-issue the same (wamid, status) tuple with a slightly
    different timestamp on retry, and we want those to dedupe.
    """
    wamid = sts.get("id")
    new_status = sts.get("status")
    if not wamid or new_status not in ("sent", "delivered", "read", "failed"):
        logger.warning(
            "wa-status: status missing id or unknown status: %r", sts
        )
        return False

    external_id = f"{wamid}:{new_status}"

    await record_event(
        source="wa-status",
        external_id=external_id,
        status="received",
        payload_hash=payload_hash,
    )

    # Convert Meta's epoch-string timestamp to a tz-aware datetime.
    moment: datetime | None = None
    ts_raw = sts.get("timestamp")
    if ts_raw:
        try:
            moment = datetime.fromtimestamp(int(ts_raw), tz=UTC)
        except (ValueError, TypeError):
            logger.warning("wa-status: bad timestamp %r; using now()", ts_raw)

    error_payload: dict[str, Any] | None = None
    errors_field = sts.get("errors") or []
    if errors_field and isinstance(errors_field, list):
        error_payload = {"errors": errors_field}

    outcome = await broadcast_delivery.apply_status_update(
        wamid=wamid,
        new_status=new_status,
        timestamp=moment,
        error_payload=error_payload,
    )
    logger.info(
        "wa-status: wamid=%s status=%s outcome=%s",
        wamid,
        new_status,
        outcome,
    )
    return outcome == broadcast_delivery.DeliveryOutcome.APPLIED
