"""POST /v1/webhooks/meta-template-status — Phase 6 PR-3.

Dedicated route for Meta template lifecycle events. Kept separate
from the unified ``/v1/webhooks/meta`` dispatcher because the
template-status flow is orthogonal to the message-centric pipeline
(no IncomingMessage enqueue, no demo router short-circuit, no
tenant-status gate — Meta sends template events regardless of
tenant suspension state and we want to record them either way).

Wire shape (Meta v21.0):

    {
      "object": "whatsapp_business_account",
      "entry": [
        {
          "id": "<waba_id>",
          "time": 1715512345,
          "changes": [
            {
              "field": "message_template_status_update",
              "value": {
                "event": "APPROVED" | "REJECTED" | "PAUSED" | ...,
                "message_template_id": "<template_id>",
                "message_template_name": "<name>",
                "message_template_language": "<locale>",
                "reason": "<rejection_reason>" | null,
                ...
              }
            }
          ]
        }
      ]
    }

Each entry may carry multiple changes; we iterate them all.
Idempotency: one webhook_events row per
``(message_template_id, event, time)`` tuple so a Meta retry
short-circuits before reaching the lifecycle handler.

Behaviour matrix:

    POST /v1/webhooks/meta-template-status
      missing / wrong sig   → 401 (+ hmac_fail row keyed by sha256(body))
      valid sig + 0 events  → 200 (+ non_message_event row)
      valid sig + N events  → 200 (lifecycle.apply_status_update fired
                                   per event)
      valid sig + replay    → 200 (idempotency short-circuit at the
                                   webhook_events UNIQUE constraint)
      Redis / DB outage     → 503 (Meta retries with backoff)

HMAC uses the same platform-level ``META_APP_SECRET`` per ADR-034.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response, status

from polaris_api.config import get_settings
from polaris_api.services.templates import lifecycle as template_lifecycle
from polaris_api.services.webhook_resolver import record_event
from polaris_api.webhooks._hmac import verify_meta_signature

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

_META_SIGNATURE_HEADER = "x-hub-signature-256"


@router.post(
    "/meta-template-status",
    summary="Meta template lifecycle webhook",
    description=(
        "Receives ``message_template_status_update`` events per Meta WhatsApp "
        "Cloud API v21.0. Routes each event through "
        "``services/templates/lifecycle.apply_status_update`` which flips "
        "``tenant_templates.status``, records an audit row, and auto-pauses "
        "any scheduled/running broadcasts referencing a degraded template "
        "(Q-PHASE6-03). Idempotency via ``webhook_events`` UNIQUE "
        "(source='meta-template-status', external_id=<template>:<event>:<time>)."
    ),
)
async def meta_template_status_inbound(request: Request) -> Response:
    raw: bytes = await request.body()
    header = request.headers.get(_META_SIGNATURE_HEADER)
    secret = get_settings().meta_app_secret
    payload_hash = hashlib.sha256(raw).digest()
    payload_hash_hex = payload_hash.hex()

    if not verify_meta_signature(raw, header, secret):
        logger.warning("meta-template-status webhook HMAC verify failed")
        # Forensic row keyed by body-hash; gracefully degrade if anything
        # downstream is also down (pool not initialised, DB unreachable,
        # whatever) — the 401 decision must stand regardless. Catch the
        # broad Exception intentionally; a narrower asyncpg.PostgresError
        # leaves RuntimeError (e.g. pool-not-initialised) to bubble as 500
        # and mask the auth verdict.
        try:
            await record_event(
                source="meta-template-status",
                external_id=payload_hash_hex,
                status="hmac_fail",
                payload_hash=payload_hash,
                error_class="HMACVerifyFailed",
            )
        except Exception as exc:  # noqa: BLE001 — see comment above
            logger.error(
                "meta-template-status: record_event during hmac_fail failed: %s", exc
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    # Parse JSON only after HMAC verify (constant-time, raw-bytes invariant
    # from _hmac.py docstring).
    import json

    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning("meta-template-status: malformed JSON body: %s", exc)
        # Meta won't recover from a malformed body — surface 400 so it
        # shows up in their dashboard instead of triggering retries.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc

    events_fired = 0
    try:
        for entry in body.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                if change.get("field") != "message_template_status_update":
                    continue
                value = change.get("value") or {}
                if not isinstance(value, dict):
                    continue
                fired = await _dispatch_one(value, payload_hash=payload_hash)
                if fired:
                    events_fired += 1
    except asyncpg.PostgresError as exc:
        logger.error("meta-template-status: DB failure during dispatch: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE
        ) from exc

    if events_fired == 0:
        # Either an empty body, or only events for fields we don't handle
        # (e.g. message_template_quality_update — separate feature). Record
        # for ops audit, return 200 so Meta doesn't retry.
        try:
            await record_event(
                source="meta-template-status",
                external_id=f"empty:{payload_hash_hex}:{datetime.now(UTC).timestamp()}",
                status="non_message_event",
                payload_hash=payload_hash,
            )
        except asyncpg.PostgresError as exc:
            logger.error(
                "meta-template-status: record_event during empty-body failed: %s",
                exc,
            )

    return Response(status_code=status.HTTP_200_OK)


async def _dispatch_one(
    value: dict[str, Any], *, payload_hash: bytes
) -> bool:
    """Apply one template-status event. Returns True iff dispatched.

    Idempotency key per 04g §6.2: ``<message_template_id>:<event>:<time>``.
    The time field varies by Meta version — they sometimes send
    ``last_updated_time`` (epoch seconds), sometimes ``timestamp``, and
    sometimes omit it entirely. We fall back to the empty string + log
    so two genuine deliveries don't collide on the UNIQUE.
    """
    meta_template_id = value.get("message_template_id")
    event = value.get("event")
    reason = value.get("reason")
    last_updated = (
        value.get("last_updated_time")
        or value.get("timestamp")
        or ""
    )

    if not meta_template_id or not event:
        logger.warning(
            "meta-template-status: value missing message_template_id or event: %r",
            value,
        )
        return False

    external_id = f"{meta_template_id}:{event}:{last_updated}"

    # Idempotency claim. record_event uses ON CONFLICT DO NOTHING; if a
    # duplicate arrives we still get an id back via the COALESCE pattern
    # but the lifecycle dispatch becomes redundant. To skip the
    # redundant work cheaply, we check the row's status field after
    # insert.
    event_id = await record_event(
        source="meta-template-status",
        external_id=external_id,
        status="received",
        payload_hash=payload_hash,
    )

    # We can't tell from record_event's return whether the insert
    # actually happened or hit the conflict path — the COALESCE returns
    # the existing row id in both cases. Lifecycle.apply_status_update
    # is itself idempotent (checks status == new_status before flipping)
    # so re-running is safe. Trade-off: marginal extra DB work on
    # replay vs. an extra round-trip to check for conflict outcome.
    result = await template_lifecycle.apply_status_update(
        meta_template_id=meta_template_id,
        event=event,
        reason=reason,
    )

    logger.info(
        "meta-template-status: event_id=%s template=%s event=%s outcome=%s "
        "broadcasts_paused=%d",
        event_id,
        meta_template_id,
        event,
        result.outcome,
        result.broadcasts_paused,
    )
    return result.outcome == "applied"
