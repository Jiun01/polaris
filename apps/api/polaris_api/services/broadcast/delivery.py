"""Broadcast delivery state machine — Phase 6 PR-7.

Translates one ``/v1/webhooks/wa-status`` event into a single state
transition on the ``broadcast_recipients`` row that owns the wamid.

State machine (forward-only):

    pending ──────► (worker sends) ──────► sent ──┬──► delivered ──► read
                                                  │
                                                  └──► failed

Rules:
  * Unknown wamid → no-op + log. Two legitimate cases:
      (a) a wa-status event for a non-broadcast send (e.g. a direct reply
          or handoff message — wamid lives on `messages` not
          `broadcast_recipients`)
      (b) a delivery event for a send that has been hard-deleted by
          DSAR cascade
    Both are operationally benign.
  * Regressions are rejected. ``read → delivered`` doesn't happen in
    practice (Meta is forward-only too), but if a delivery event
    arrives out-of-order via webhook replay we keep the more-advanced
    state. This protects the broadcasts counter from double-counting.
  * Counters on ``broadcasts`` (delivered_count, read_count,
    failed_count) increment ONLY on a real status transition — never
    on a no-op replay.

Resolution path:
  Look up the recipient row by wamid across all tenants via the
  service pool. The wamid -> tenant binding is the row's own
  tenant_id; once we have that, we set ``app.current_tenant_id``
  GUC explicitly before any audit/event writes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import asyncpg

from polaris_api.db.pools import get_service_pool

logger = logging.getLogger(__name__)


# Meta wire vocabulary for the wa-status webhook. Per Cloud API
# v21.0 + 04g §1.2 doc-lookup table.
WaStatusEvent = Literal["sent", "delivered", "read", "failed"]


# Per-state rank — higher value = further along. A transition is
# allowed iff new_rank > current_rank. Failed sits at rank 99 so it
# always overrides forward states (a delivered message can still
# fail if the customer's number became invalid before read).
_RANK: dict[str, int] = {
    "pending": 0,
    "skipped": 1,  # terminal-ish; we never transition INTO skipped via wa-status
    "sent": 10,
    "delivered": 20,
    "read": 30,
    "failed": 99,
}


class DeliveryOutcome:
    """Returned for logging / test assertions; the router only
    cares about HTTP 200 OK semantics."""

    APPLIED = "applied"
    NOOP_UNKNOWN_WAMID = "noop_unknown_wamid"
    NOOP_REGRESSION = "noop_regression"
    NOOP_IDEMPOTENT = "noop_idempotent"


async def apply_status_update(
    *,
    wamid: str,
    new_status: WaStatusEvent,
    timestamp: datetime | None = None,
    error_payload: dict[str, Any] | None = None,
) -> str:
    """Apply one wa-status event. Returns a DeliveryOutcome string.

    Idempotency upstream: the webhook router writes a
    ``webhook_events`` row keyed on
    ``(source='wa-status', external_id=f"{wamid}:{status}")`` BEFORE
    calling this — so a Meta retry of the same event never reaches
    us. Internal idempotency (rank check) covers webhook_events row
    being inserted out-of-order vs the actual delivery sequence.
    """
    moment = timestamp if timestamp is not None else datetime.now(UTC)
    pool = get_service_pool()
    async with pool.acquire() as conn, conn.transaction():
        # Cross-tenant resolve: wamid -> recipient row -> tenant_id.
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, broadcast_id, status, channel,
                   channel_user_id
              FROM broadcast_recipients
             WHERE wa_message_id = $1
            """,
            wamid,
        )
        if row is None:
            logger.info(
                "broadcast.delivery: unknown wamid %s; ignoring",
                wamid,
            )
            return DeliveryOutcome.NOOP_UNKNOWN_WAMID

        tenant_id: UUID = row["tenant_id"]
        recipient_id: UUID = row["id"]
        broadcast_id: UUID = row["broadcast_id"]
        current_status: str = row["status"]

        # Same-state event = idempotent no-op.
        if current_status == new_status:
            return DeliveryOutcome.NOOP_IDEMPOTENT

        # Forward-only guard.
        current_rank = _RANK.get(current_status, 0)
        new_rank = _RANK.get(new_status, 0)
        if new_rank <= current_rank and new_status != "failed":
            logger.info(
                "broadcast.delivery: rejecting regression for wamid=%s "
                "current=%s new=%s",
                wamid,
                current_status,
                new_status,
            )
            return DeliveryOutcome.NOOP_REGRESSION

        # Bind GUC so the audit chain (and any future RLS-scoped
        # downstream writes) see the right tenant. Cross-tenant
        # resolution was the only step that needed BYPASSRLS.
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)",
            str(tenant_id),
        )

        # Update the recipient row.
        await _update_recipient(
            conn,
            recipient_id=recipient_id,
            new_status=new_status,
            moment=moment,
            error_payload=error_payload,
        )

        # Update broadcasts counters. Per-status counter +1 ONLY on
        # transitions that touch a counter; the 'sent' transition
        # already incremented sent_count in worker.py — wa-status
        # 'sent' should not double-count.
        if new_status == "delivered":
            await conn.execute(
                """
                UPDATE broadcasts
                   SET delivered_count = delivered_count + 1, updated_at = now()
                 WHERE id = $1
                """,
                broadcast_id,
            )
        elif new_status == "read":
            await conn.execute(
                """
                UPDATE broadcasts
                   SET read_count = read_count + 1, updated_at = now()
                 WHERE id = $1
                """,
                broadcast_id,
            )
        elif new_status == "failed":
            # Only increment failed_count if we're transitioning from a
            # NON-terminal state. A row in 'delivered' that flips to
            # 'failed' (post-delivery customer-side failure) still
            # increments failed_count but we leave delivered_count
            # alone — operationally, the broadcast did deliver, the
            # downstream signal is informational.
            await conn.execute(
                """
                UPDATE broadcasts
                   SET failed_count = failed_count + 1, updated_at = now()
                 WHERE id = $1
                """,
                broadcast_id,
            )

    logger.info(
        "broadcast.delivery: tenant=%s broadcast=%s recipient=%s "
        "%s -> %s",
        tenant_id,
        broadcast_id,
        recipient_id,
        current_status,
        new_status,
    )
    return DeliveryOutcome.APPLIED


async def _update_recipient(
    conn: asyncpg.Connection,
    *,
    recipient_id: UUID,
    new_status: WaStatusEvent,
    moment: datetime,
    error_payload: dict[str, Any] | None,
) -> None:
    """Apply the per-status column writes.

    Each terminal state has its own ``*_at`` timestamp column on
    broadcast_recipients. ``failed_at`` + ``failure_payload`` populate
    only on the failed transition; the prior columns are left
    untouched so the timeline reconstructs cleanly.
    """
    if new_status == "delivered":
        await conn.execute(
            """
            UPDATE broadcast_recipients
               SET status = 'delivered',
                   delivered_at = $2,
                   updated_at = now()
             WHERE id = $1
            """,
            recipient_id,
            moment,
        )
    elif new_status == "read":
        await conn.execute(
            """
            UPDATE broadcast_recipients
               SET status = 'read',
                   read_at = $2,
                   updated_at = now()
             WHERE id = $1
            """,
            recipient_id,
            moment,
        )
    elif new_status == "failed":
        await conn.execute(
            """
            UPDATE broadcast_recipients
               SET status = 'failed',
                   failed_at = $2,
                   failure_payload = $3::jsonb,
                   updated_at = now()
             WHERE id = $1
            """,
            recipient_id,
            moment,
            # dict DIRECTLY — JSONB codec serialises once (services/audit.py).
            error_payload or {},
        )
    elif new_status == "sent":
        # wa-status 'sent' is the same signal worker.py already wrote
        # — we shouldn't see this in practice. If it arrives, update
        # sent_at (in case worker.py set it to its own clock vs
        # Meta's authoritative one) but don't double-count.
        await conn.execute(
            """
            UPDATE broadcast_recipients
               SET status = 'sent',
                   sent_at = COALESCE(sent_at, $2),
                   updated_at = now()
             WHERE id = $1
            """,
            recipient_id,
            moment,
        )
