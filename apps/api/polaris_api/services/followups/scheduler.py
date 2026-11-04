"""Follow-up scheduling — Phase 13 follow-ups PR-1.

Called from the inbound pipeline (anon pool, RLS, tenant GUC set):

  * ``cancel_pending_followup`` — on every inbound, before reply logic.
    The customer is active again, so any pending nudge is moot.
  * ``maybe_schedule_followup`` — after a general-support bot reply, when
    the conversation qualifies. Gathers its own signals (one query), so
    the pipeline just makes one call.

Both run inside the pipeline's transaction (GUC set), so RLS scopes the
writes; no service pool here. Idempotent upsert keeps "one pending
follow-up per conversation".
"""

from __future__ import annotations

import logging
from uuid import UUID
from zoneinfo import ZoneInfoNotFoundError

import asyncpg

from polaris_api.services.followups.eligibility import (
    ScheduleSignals,
    is_followup_enabled,
    should_schedule_followup,
)
from polaris_api.services.followups.timing import compute_followup_send_time

logger = logging.getLogger(__name__)

# Fallback when a tenant has no (or an invalid) timezone — Polaris' market.
_FALLBACK_TZ = "Asia/Kuala_Lumpur"


async def cancel_pending_followup(
    conn: asyncpg.Connection, *, tenant_id: UUID, conversation_id: UUID
) -> None:
    """Delete the conversation's pending follow-up, if any (customer is
    active again). No-op when none exists."""
    await conn.execute(
        """
        DELETE FROM followup_jobs
         WHERE tenant_id = $1 AND conversation_id = $2 AND sent_at IS NULL
        """,
        tenant_id,
        conversation_id,
    )


async def maybe_schedule_followup(
    conn: asyncpg.Connection, *, tenant_id: UUID, conversation_id: UUID
) -> bool:
    """Schedule a follow-up for this conversation if it qualifies.

    Returns True if a job was (re)scheduled. Call AFTER persisting the
    bot's reply (so the last message is the assistant's) and inside the
    pipeline transaction. Self-contained: one query for all signals.
    """
    row = await conn.fetchrow(
        """
        SELECT
            c.channel,
            c.handoff_at,
            t.settings,
            t.timezone,
            (SELECT max(m.created_at) FROM messages m
              WHERE m.conversation_id = c.id AND m.role = 'user') AS last_inbound_at,
            (SELECT m2.role FROM messages m2
              WHERE m2.conversation_id = c.id
              ORDER BY m2.created_at DESC LIMIT 1)               AS last_message_role,
            (SELECT am.opted_out_at FROM audience_members am
              WHERE am.tenant_id = c.tenant_id
                AND am.phone_number_e164 = c.channel_user_id)    AS opted_out_at
          FROM conversations c
          JOIN tenants t ON t.id = c.tenant_id
         WHERE c.id = $1 AND c.tenant_id = $2
        """,
        conversation_id,
        tenant_id,
    )
    if row is None:
        return False

    signals = ScheduleSignals(
        enabled=is_followup_enabled(row["settings"]),
        channel=str(row["channel"]),
        handoff_at=row["handoff_at"],
        last_inbound_at=row["last_inbound_at"],
        last_message_role=row["last_message_role"],
        # Don't schedule for opted-out customers — don't even create the row
        # (data minimisation), beyond the worker's send-time re-check.
        opted_out=row["opted_out_at"] is not None,
    )
    if not should_schedule_followup(signals):
        return False

    anchor = signals.last_inbound_at
    if anchor is None:  # guaranteed non-None by the gate; explicit for -O safety
        return False
    tenant_tz = row["timezone"] or _FALLBACK_TZ
    try:
        send_at = compute_followup_send_time(
            anchor_inbound_at=anchor, tenant_tz=tenant_tz
        )
    except ZoneInfoNotFoundError:
        logger.warning(
            "followups: bad tenant timezone %r tenant=%s — using %s",
            tenant_tz, tenant_id, _FALLBACK_TZ,
        )
        send_at = compute_followup_send_time(
            anchor_inbound_at=anchor, tenant_tz=_FALLBACK_TZ
        )
    if send_at is None:
        # No humane in-window slot (whole window is quiet) → skip.
        return False

    await conn.execute(
        """
        INSERT INTO followup_jobs
            (tenant_id, conversation_id, anchor_inbound_at, scheduled_at)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (conversation_id) WHERE sent_at IS NULL
        DO UPDATE SET
            anchor_inbound_at = EXCLUDED.anchor_inbound_at,
            scheduled_at      = EXCLUDED.scheduled_at,
            attempts          = 0,
            last_error        = NULL,
            updated_at        = now()
        """,
        tenant_id,
        conversation_id,
        signals.last_inbound_at,
        send_at,
    )
    logger.info(
        "followups: scheduled tenant=%s conv=%s at=%s",
        tenant_id, conversation_id, send_at.isoformat(),
    )
    return True
