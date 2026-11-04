"""Per-recipient compliance gate — Phase 6 PR-6.

Called BEFORE every outbound send by the broadcast worker. Returns
ComplianceGateResult(allowed=True) iff the send may proceed; False
otherwise with skip_reason populated.

Three rules per 04g §5.7:

  1. STOP keyword — pre-existing STOP block in ``stop_blocks``.
     **GA gap**: the stop_blocks table is owned by Phase 2's STOP
     handler (ADR-010) which has not yet been built (see
     Q-PHASE6-06). Rule 1 is implemented as a no-op stub that
     returns "not blocked" + relies on rule 2 to catch STOP'd
     contacts via ``audience_members.opted_out_at``. When Phase 2
     ships the STOP handler, it should:
       (a) write a stop_blocks row, AND
       (b) call ``services/audience/opt_out.mark_opted_out`` with
           reason='stop_keyword' so audience_members.opted_out_at
           is set.
     The Phase 6 worker's rule 2 catches (b); rule 1's stub
     remains correct because (a) is a defence-in-depth signal
     that isn't load-bearing for outbound compliance.

  2. ``audience_members.opted_out_at IS NOT NULL`` — the contact
     has opted out (manual, STOP, or DSAR cascade). Skip.

  3. 24h-window rule for IG/Messenger non-template messages
     (MARKETING category specifically). Free-form messages outside
     the 24h customer-response window violate Meta policy. Skip
     unless the template is UTILITY/AUTHENTICATION (those bypass
     the window per Meta's policy doc) or the channel is WhatsApp
     (templates ARE the window-reopen mechanism on WhatsApp).

Env switch: ``BROADCAST_24H_WINDOW_CHECK_ENABLED=false`` disables
rule 3. Sandbox testing only — disabling in prod is a Meta-policy
violation.

The compliance gate uses the request-scoped connection passed in
by the worker; the worker has already set ``app.current_tenant_id``
GUC for RLS scoping on the audience_members + messages reads.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


SkipReason = Literal[
    "stop_keyword",
    "opted_out",
    "window_closed",
]


@dataclass(frozen=True)
class ComplianceGateResult:
    """Outcome of one compliance check.

    ``allowed=True`` → worker proceeds with the send.
    ``allowed=False`` → worker flips the recipient to
    status='skipped' with skip_reason populated.
    """

    allowed: bool
    skip_reason: SkipReason | None = None


_24H = timedelta(hours=24)


def _window_check_enabled() -> bool:
    """Read BROADCAST_24H_WINDOW_CHECK_ENABLED. Default TRUE.

    Anything other than ``false`` (case-insensitive) keeps the check
    on; deliberately strict so a typo like ``BROADCAST_24H_WINDOW_
    CHECK_ENABLED=fakse`` doesn't silently disable a compliance
    rule.
    """
    raw = os.getenv("BROADCAST_24H_WINDOW_CHECK_ENABLED", "true")
    return raw.strip().lower() != "false"


async def check(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    channel: str,
    channel_user_id: str,
    template_category: str,
    now: datetime | None = None,
) -> ComplianceGateResult:
    """Run the three rules; return on first deny."""
    moment = now if now is not None else datetime.now(UTC)

    # Rule 2: opted-out (also catches Phase 2's STOP handler outcome
    # since the STOP handler writes opted_out_at via the same path).
    optout_row = await conn.fetchrow(
        """
        SELECT opted_out_at, opted_out_reason
          FROM audience_members
         WHERE tenant_id = $1
           AND channel = $2
           AND channel_user_id = $3
           AND opted_out_at IS NOT NULL
         LIMIT 1
        """,
        tenant_id,
        channel,
        channel_user_id,
    )
    if optout_row is not None:
        # If the opt-out was driven by the STOP keyword we surface
        # it as the more specific reason for the portal recipient
        # detail view — useful when an agent asks "why was this
        # contact skipped?".
        reason: SkipReason = (
            "stop_keyword"
            if optout_row["opted_out_reason"] == "stop_keyword"
            else "opted_out"
        )
        return ComplianceGateResult(allowed=False, skip_reason=reason)

    # Rule 3: 24h-window for Messenger/IG-Login non-UTILITY templates.
    # WhatsApp templates bypass — templates ARE the window-reopen
    # mechanism on WhatsApp per Meta policy. UTILITY +
    # AUTHENTICATION categories also bypass per Meta policy doc.
    # instagram_login (graph.instagram.com) has the same 24h reply window.
    if (
        _window_check_enabled()
        and channel in ("messenger", "instagram_login")
        and template_category == "MARKETING"
    ):
        last_inbound = await conn.fetchval(
            """
            SELECT MAX(m.created_at)
              FROM conversations c
              JOIN messages m ON m.conversation_id = c.id
             WHERE c.tenant_id = $1
               AND c.channel = $2
               AND c.channel_user_id = $3
               AND m.role = 'user'
            """,
            tenant_id,
            channel,
            channel_user_id,
        )
        if last_inbound is None or (moment - last_inbound) > _24H:
            return ComplianceGateResult(allowed=False, skip_reason="window_closed")

    return ComplianceGateResult(allowed=True)
