"""Handoff partial-failure recovery — Phase 7 PR-3.

Q-PHASE7-04: when a handoff's customer-facing message succeeded but
the Chatwoot path (status flip / assignment / private note) failed,
``services/chatwoot/handoff.py::initiate_handoff`` marks the
conversation with ``handoff_pending_recovery_at`` + ``_reason``. This
module posts the deferred recovery note on the NEXT customer inbound
for that conversation.

Why deferred? Two failure modes drove the design:

  1. Chatwoot was transiently unreachable (5xx, timeout). When it
     recovers, posting a private note "(handoff requested at {ts}
     but Chatwoot was unreachable)" gives the agent the audit trail
     they'd otherwise miss.

  2. Customer never followed up. Without a deferred post, the
     conversation in Chatwoot looks unremarkable to the agent — they
     have no way to know the bot tried to escalate and failed.
     Tying the recovery note to the customer's next inbound means
     the agent only sees the explainer when there's still a customer
     who needs an answer.

This module is called from ``services/pipeline.py`` after conversation
resolution + before the flow / LLM dispatch. Non-blocking: a failure
to post the recovery note doesn't stop the inbound from processing
(otherwise a Chatwoot outage would block ALL inbound traffic for that
tenant). Failures log at WARNING and leave the marker in place so a
subsequent inbound retries.

Silent-failure audit fix A4 (2026-07-02): the original combined
``post_recovery_note_if_pending`` did a DB read + an HTTP POST + a DB
write all on the SAME ``conn``, inside the pipeline's open
``conn.transaction()`` block — a slow/unreachable Chatwoot held a
pooled connection for the whole HTTP round trip on every message with
a pending marker. Split into ``check_pending_recovery`` (DB-only,
cheap, stays inline in the transaction) and
``post_recovery_note_deferred`` (HTTP post + marker clear, runs via
the ``deferred_cw`` mechanism AFTER the transaction commits and the
connection is released — mirrors ``pipeline.py``'s
``_post_voice_note_deferred``). The deferred form self-acquires a
FRESH connection only for the marker-clear UPDATE, and only after the
post succeeds, so the HTTP call itself never holds a pool slot.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg

from polaris_api.config import get_settings
from polaris_api.services.chatwoot.client import (
    ChatwootClient,
    ChatwootError,
)

logger = logging.getLogger(__name__)


_FETCH_PENDING_SQL = """
SELECT
    c.handoff_pending_recovery_at      AS recovery_at,
    c.handoff_pending_recovery_reason  AS recovery_reason,
    c.chatwoot_conversation_id         AS chatwoot_conversation_id,
    t.chatwoot_account_id              AS chatwoot_account_id
FROM conversations c
JOIN tenants t ON t.id = c.tenant_id
WHERE c.id = $1 AND c.tenant_id = $2
"""


_CLEAR_MARKER_SQL = """
UPDATE conversations
   SET handoff_pending_recovery_at     = NULL,
       handoff_pending_recovery_reason = NULL
 WHERE id = $1 AND tenant_id = $2
"""


@dataclass(frozen=True, slots=True)
class PendingRecovery:
    """A pending recovery marker resolved inline (DB-only, cheap) during the
    pipeline transaction. Carries everything the deferred post needs so the
    deferred call has zero DB dependency of its own (mirrors
    ``_post_voice_note_deferred``'s conn-independent shape)."""

    chatwoot_account_id: int
    chatwoot_conversation_id: int
    note: str


async def check_pending_recovery(
    *,
    conn: asyncpg.Connection,
    tenant_id: UUID,
    conversation_id: UUID,
) -> PendingRecovery | None:
    """Read-only check for a pending recovery marker. DB-only, cheap — safe to
    call inline inside the pipeline transaction (no HTTP, no lock-holding risk).

    Returns ``None`` when there's nothing to do (no marker, or the chatwoot
    link is missing — same cases the old combined function short-circuited
    on). Returns a ``PendingRecovery`` payload when the caller should defer a
    ``post_recovery_note_deferred(...)`` call after the transaction commits.
    """
    row = await conn.fetchrow(_FETCH_PENDING_SQL, conversation_id, tenant_id)
    if row is None:
        return None

    recovery_at: datetime | None = row["recovery_at"]
    if recovery_at is None:
        return None

    recovery_reason = row["recovery_reason"] or "unknown"
    chatwoot_account_id = row["chatwoot_account_id"]
    chatwoot_conversation_id = row["chatwoot_conversation_id"]
    if not isinstance(chatwoot_account_id, int) or not isinstance(
        chatwoot_conversation_id, int
    ):
        logger.warning(
            "handoff_recovery: marker set but chatwoot link missing "
            "tenant_id=%s conversation_id=%s",
            tenant_id,
            conversation_id,
        )
        return None

    settings = get_settings()
    # format_map + defaultdict guards against an operator misconfiguring
    # HANDOFF_RECOVERY_NOTE_TEMPLATE with an unknown placeholder. Without
    # this, an extra `{foo}` in the template raises KeyError on every
    # inbound for a flagged conversation, leaving the marker stuck forever
    # (security review Nit 3). Unknown placeholders quietly resolve to
    # empty string instead.
    fmt_vars: dict[str, str] = defaultdict(str)
    fmt_vars["ts"] = recovery_at.isoformat()
    fmt_vars["reason"] = recovery_reason
    note = settings.handoff_recovery_note_template.format_map(fmt_vars)

    return PendingRecovery(
        chatwoot_account_id=chatwoot_account_id,
        chatwoot_conversation_id=chatwoot_conversation_id,
        note=note,
    )


async def post_recovery_note_deferred(
    *,
    pending: PendingRecovery,
    tenant_id: UUID,
    conversation_id: UUID,
    chatwoot: ChatwootClient | None,
    pool: asyncpg.Pool,
) -> None:
    """Post the recovery note + clear the marker (deferred form).

    Runs AFTER the pipeline transaction commits and its connection is
    released — the Chatwoot HTTP call no longer holds a pooled connection.
    Self-acquires a FRESH connection only for the marker-clear UPDATE (a
    short, cheap statement), and only if the post succeeded. Best-effort: a
    still-unreachable Chatwoot leaves the marker in place for the next
    inbound to retry (identical failure-mode contract as before the split).
    """
    if chatwoot is None:
        return
    try:
        await chatwoot.post_private_note(
            account_id=pending.chatwoot_account_id,
            conversation_id=pending.chatwoot_conversation_id,
            content=pending.note,
        )
    except ChatwootError as exc:
        logger.warning(
            "handoff_recovery: failed to post recovery note "
            "tenant_id=%s conversation_id=%s err=%s",
            tenant_id,
            conversation_id,
            type(exc).__name__,
        )
        return
    except Exception:  # noqa: BLE001 — same posture as above for unexpected
        logger.exception(
            "handoff_recovery: unexpected exception posting recovery note "
            "tenant_id=%s conversation_id=%s",
            tenant_id,
            conversation_id,
        )
        return

    try:
        async with pool.acquire() as conn:
            await conn.execute(_CLEAR_MARKER_SQL, conversation_id, tenant_id)
    except Exception:  # noqa: BLE001 — post succeeded; a clear-marker failure
        # just means the NEXT inbound re-posts a duplicate note (idempotent
        # enough — an extra audit note, not a customer-facing failure).
        logger.warning(
            "handoff_recovery: posted note but failed to clear marker "
            "tenant_id=%s conversation_id=%s",
            tenant_id,
            conversation_id,
            exc_info=True,
        )
        return

    logger.info(
        "handoff_recovery: posted recovery note tenant_id=%s conversation_id=%s",
        tenant_id,
        conversation_id,
    )
