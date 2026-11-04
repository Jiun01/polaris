"""POST /v1/webhooks/wa-account — Phase 6 PR-7.

MM Lite onboarding lifecycle from Meta. Wire shape:

    {
      "object": "whatsapp_business_account",
      "entry": [
        {
          "id": "<waba_id>",
          "changes": [
            {
              "field": "account_update",
              "value": {
                "event": "AD_ACCOUNT_LINKED" | "ONBOARDING_COMPLETED"
                       | "AD_ACCOUNT_UNLINKED",
                "phone_number_id": "<pid>",
                "ad_account_id": "<aaid>",   // present on LINKED
                "timestamp": 1715512345
              }
            }
          ]
        }
      ]
    }

Resolves ``phone_number_id`` to a ``tenant_channels`` row via the
service pool (cross-tenant lookup; phone_number_id is globally
unique within Meta's namespace), then sets app.current_tenant_id
GUC before mutating.

Status transitions (per 04g §3.4 mm_lite_status vocabulary):
    AD_ACCOUNT_LINKED       -> 'IN_PROGRESS'  (+ stamp mm_lite_ad_account_id)
    ONBOARDING_COMPLETED    -> 'ONBOARDED'    (+ stamp mm_lite_onboarded_at)
    AD_ACCOUNT_UNLINKED     -> 'UNLINKED'

Unknown event names log + no-op (Meta may add new states).

Idempotency via webhook_events UNIQUE on
``(source='wa-account', external_id=<pid>:<event>:<timestamp>)``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response, status

from polaris_api.config import get_settings
from polaris_api.db.pools import get_service_pool
from polaris_api.services.webhook_resolver import record_event
from polaris_api.webhooks._hmac import verify_meta_signature

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

_META_SIGNATURE_HEADER = "x-hub-signature-256"

# Meta vocab -> our mm_lite_status enum.
_EVENT_TO_STATUS: dict[str, str] = {
    "AD_ACCOUNT_LINKED": "IN_PROGRESS",
    "ONBOARDING_COMPLETED": "ONBOARDED",
    "AD_ACCOUNT_UNLINKED": "UNLINKED",
}


@router.post(
    "/wa-account",
    summary="Meta WhatsApp account / MM Lite onboarding webhook",
    description=(
        "Receives ``account_update`` events (AD_ACCOUNT_LINKED, "
        "ONBOARDING_COMPLETED, AD_ACCOUNT_UNLINKED) per Meta WhatsApp "
        "Cloud API. Resolves phone_number_id to a tenant_channels row "
        "and updates the mm_lite_* columns. The mm_lite_status drives "
        "broadcast endpoint selection in services/broadcast/worker — "
        "ONBOARDED + MARKETING template -> /marketing_messages."
    ),
)
async def wa_account_inbound(request: Request) -> Response:
    raw: bytes = await request.body()
    header = request.headers.get(_META_SIGNATURE_HEADER)
    secret = get_settings().meta_app_secret
    payload_hash = hashlib.sha256(raw).digest()
    payload_hash_hex = payload_hash.hex()

    if not verify_meta_signature(raw, header, secret):
        logger.warning("wa-account webhook HMAC verify failed")
        try:
            await record_event(
                source="wa-account",
                external_id=payload_hash_hex,
                status="hmac_fail",
                payload_hash=payload_hash,
                error_class="HMACVerifyFailed",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "wa-account: record_event during hmac_fail failed: %s", exc
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning("wa-account: malformed JSON body: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc

    events_fired = 0
    try:
        for entry in body.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                if change.get("field") != "account_update":
                    continue
                value = change.get("value") or {}
                if not isinstance(value, dict):
                    continue
                fired = await _dispatch_one(value, payload_hash=payload_hash)
                if fired:
                    events_fired += 1
    except asyncpg.PostgresError as exc:
        logger.error("wa-account: DB failure during dispatch: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE
        ) from exc

    if events_fired == 0:
        try:
            await record_event(
                source="wa-account",
                external_id=f"empty:{payload_hash_hex}:{datetime.now(UTC).timestamp()}",
                status="non_message_event",
                payload_hash=payload_hash,
            )
        except asyncpg.PostgresError as exc:
            logger.error(
                "wa-account: record_event during empty-body failed: %s", exc
            )

    return Response(status_code=status.HTTP_200_OK)


async def _dispatch_one(
    value: dict[str, Any], *, payload_hash: bytes
) -> bool:
    """Apply one account_update event. Returns True iff dispatched."""
    phone_number_id = value.get("phone_number_id")
    event = value.get("event")
    timestamp = value.get("timestamp")
    ad_account_id = value.get("ad_account_id")

    if not phone_number_id or not event:
        logger.warning(
            "wa-account: missing phone_number_id or event: %r", value
        )
        return False

    new_status = _EVENT_TO_STATUS.get(event)
    if new_status is None:
        logger.warning(
            "wa-account: unknown event %s for phone_number_id %s — ignored",
            event,
            phone_number_id,
        )
        return False

    external_id = f"{phone_number_id}:{event}:{timestamp or ''}"
    await record_event(
        source="wa-account",
        external_id=external_id,
        status="received",
        payload_hash=payload_hash,
    )

    return await _apply_mm_lite_update(
        phone_number_id=phone_number_id,
        new_status=new_status,
        ad_account_id=ad_account_id,
        event=event,
    )


async def _apply_mm_lite_update(
    *,
    phone_number_id: str,
    new_status: str,
    ad_account_id: str | None,
    event: str,
) -> bool:
    """Resolve PID -> tenant_channels row and update mm_lite_* columns.

    Returns False if no channel matches the PID (legitimate when the
    tenant has disconnected the channel between Meta firing the event
    and us processing it). True on a successful UPDATE.
    """
    pool = get_service_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT id, tenant_id FROM tenant_channels WHERE phone_number_id = $1",
            phone_number_id,
        )
        if row is None:
            logger.info(
                "wa-account: phone_number_id %s not found in tenant_channels; "
                "ignoring %s",
                phone_number_id,
                event,
            )
            return False

        channel_id: UUID = row["id"]
        tenant_id: UUID = row["tenant_id"]

        # Bind GUC for any downstream RLS-aware writes (audit/event row).
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)",
            str(tenant_id),
        )

        # ONBOARDING_COMPLETED is the only transition that stamps
        # mm_lite_onboarded_at — and only the FIRST time. Subsequent
        # re-ONBOARDED events (e.g. unlink -> relink) don't overwrite
        # the first-onboarded timestamp; the cost dashboard
        # (per finance-billing-ops audit) uses it for MM Lite
        # eligibility duration.
        if new_status == "ONBOARDED":
            await conn.execute(
                """
                UPDATE tenant_channels
                   SET mm_lite_status = $2,
                       mm_lite_status_updated_at = now(),
                       mm_lite_onboarded_at = COALESCE(mm_lite_onboarded_at, now())
                 WHERE id = $1
                """,
                channel_id,
                new_status,
            )
        elif new_status == "IN_PROGRESS":
            await conn.execute(
                """
                UPDATE tenant_channels
                   SET mm_lite_status = $2,
                       mm_lite_status_updated_at = now(),
                       mm_lite_ad_account_id = COALESCE($3, mm_lite_ad_account_id)
                 WHERE id = $1
                """,
                channel_id,
                new_status,
                ad_account_id,
            )
        else:  # UNLINKED
            await conn.execute(
                """
                UPDATE tenant_channels
                   SET mm_lite_status = $2,
                       mm_lite_status_updated_at = now()
                 WHERE id = $1
                """,
                channel_id,
                new_status,
            )

        # Persist an outbound event row (deferred dispatch — v3.0).
        # Same guard as templates / opt_out paths.
        try:
            await conn.execute(
                """
                INSERT INTO webhook_events_outbound
                    (tenant_id, event_type, payload, created_at)
                VALUES ($1, $2, $3::jsonb, now())
                """,
                tenant_id,
                f"channel.mm_lite.{new_status.lower()}",
                # dict DIRECTLY — the JSONB codec (pools.py) serialises once;
                # json.dumps would double-encode into a JSONB string (audit.py).
                {
                    "tenant_channel_id": str(channel_id),
                    "phone_number_id": phone_number_id,
                    "event": event,
                    "ad_account_id": ad_account_id,
                },
            )
        except asyncpg.UndefinedTableError:
            logger.warning(
                "wa-account: webhook_events_outbound missing; "
                "skipping outbound event persistence for tenant=%s",
                tenant_id,
            )

    logger.info(
        "wa-account: tenant=%s channel=%s mm_lite_status=%s (event=%s)",
        tenant_id,
        channel_id,
        new_status,
        event,
    )
    return True
