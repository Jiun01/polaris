"""Template lifecycle webhook handler — Phase 6 PR-3.

Translates a single Meta ``message_template_status_update`` webhook
event into:

  1. Resolution: meta_template_id → tenant_id (via repo, BYPASSRLS).
  2. Status flip: UPDATE ``tenant_templates.status`` +
     ``rejection_reason`` (Meta's free-text reason on REJECTED).
  3. Audit row: ``services/audit.record(...)`` with
     ``action='template.status_change'``.
  4. Outbound event row: persisted into ``webhook_events_outbound``
     for v3.0 dispatch (see ADR-035 Amendment evening — Phase 6 only
     persists, never pushes).
  5. **Auto-pause cascade** (Q-PHASE6-03): if the new status is
     ``rejected`` / ``paused`` / ``disabled``, scan
     ``broadcasts WHERE template_id = $1 AND status IN ('scheduled',
     'running')`` and pause each — record ``broadcasts.error.code =
     'TEMPLATE_STATUS_CHANGED'``. Without this, a broadcast worker
     would keep sending against a no-longer-approved template and
     get 4xx'd per recipient.

Map Meta's webhook event vocabulary to our enum:
   APPROVED         -> approved
   REJECTED         -> rejected
   PAUSED           -> paused          (Meta-side autopause)
   DISABLED         -> disabled
   FLAGGED          -> paused          (treat as soft-paused; Meta's
                                       FLAGGED is a precursor to DISABLED
                                       and operationally equivalent to
                                       PAUSED from our side)
   PENDING_DELETION -> disabled        (terminal from our side)
   PENDING          -> pending         (rare — usually arrives before
                                       our submit-200 returns)

Unknown event values are logged + ignored (not raised). Meta has
historically added new lifecycle events without warning; rejecting
them here would break the webhook handler and back up Meta's retry
queue.

The handler runs under the BYPASSRLS service pool so the
cross-tenant resolution works; it explicitly sets the GUC before
calling repo functions that mutate.
"""

from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import UUID

import asyncpg

from polaris_api.db.pools import get_service_pool
from polaris_api.services import audit
from polaris_api.services.templates import repo

logger = logging.getLogger(__name__)


# Meta vocab → our status enum.
_EVENT_TO_STATUS: dict[str, str] = {
    "APPROVED": "approved",
    "REJECTED": "rejected",
    "PAUSED": "paused",
    "DISABLED": "disabled",
    "FLAGGED": "paused",
    "PENDING_DELETION": "disabled",
    "PENDING": "pending",
}

# Status values that require the broadcast auto-pause cascade.
_DEGRADED_STATUSES: frozenset[str] = frozenset({"rejected", "paused", "disabled"})


class LifecycleResult:
    """Outcome of a lifecycle event apply. Returned for logging /
    test assertions; the handler itself doesn't need it.
    """

    def __init__(
        self,
        *,
        outcome: Literal["applied", "noop_unknown_event", "noop_template_missing"],
        tenant_id: UUID | None = None,
        template_id: UUID | None = None,
        new_status: str | None = None,
        broadcasts_paused: int = 0,
    ) -> None:
        self.outcome = outcome
        self.tenant_id = tenant_id
        self.template_id = template_id
        self.new_status = new_status
        self.broadcasts_paused = broadcasts_paused


async def apply_status_update(
    *,
    meta_template_id: str,
    event: str,
    reason: str | None,
) -> LifecycleResult:
    """Apply one Meta lifecycle event end-to-end.

    Idempotency upstream: the webhook router writes a
    ``webhook_events`` row keyed on
    ``(source='meta-template-status', external_id=<message_template_id>:<event>:<last_updated_time>)``
    BEFORE calling this — so the second delivery of the same payload
    won't reach us at all. Internal to this function we still
    de-duplicate at the row-update level (no-op if status already
    matches), guarding against a webhook-events row that was inserted
    out-of-order.

    ``reason`` is Meta's ``rejected_reason`` text for REJECTED, or
    ``None`` for other events. Stored verbatim into
    ``tenant_templates.rejection_reason``.
    """
    new_status = _EVENT_TO_STATUS.get(event)
    if new_status is None:
        logger.warning(
            "template lifecycle: unknown event %s for template %s — ignored",
            event,
            meta_template_id,
        )
        return LifecycleResult(outcome="noop_unknown_event")

    pool = get_service_pool()
    async with pool.acquire() as conn, conn.transaction():
        template = await repo.get_template_by_meta_id(conn, meta_template_id)
        if template is None:
            # The template was deleted (or never existed). This can
            # legitimately fire when a tenant deletes a template
            # locally before Meta's webhook arrives — we ignore.
            logger.info(
                "template lifecycle: meta_template_id %s not found locally; "
                "event %s ignored",
                meta_template_id,
                event,
            )
            return LifecycleResult(outcome="noop_template_missing")

        tenant_id: UUID = template["tenant_id"]
        template_id: UUID = template["id"]
        old_status: str = template["status"]

        if old_status == new_status and (
            template["rejection_reason"] or "" == (reason or "")
        ):
            logger.info(
                "template lifecycle: tenant=%s template=%s already at status=%s; "
                "no-op",
                tenant_id,
                template_id,
                new_status,
            )
            return LifecycleResult(
                outcome="applied",
                tenant_id=tenant_id,
                template_id=template_id,
                new_status=new_status,
                broadcasts_paused=0,
            )

        # Bind GUC so RLS-aware downstream functions (notably audit)
        # see the right tenant.
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)",
            str(tenant_id),
        )

        await repo.update_status(
            conn,
            template_id=template_id,
            status=new_status,
            rejection_reason=reason,
        )

        await audit.record(
            conn,
            tenant_id,
            None,  # actor: Meta — no Polaris user
            "template.status_change",
            before={"status": old_status, "rejection_reason": template["rejection_reason"]},
            after={"status": new_status, "rejection_reason": reason},
        )

        broadcasts_paused = 0
        if new_status in _DEGRADED_STATUSES:
            broadcasts_paused = await _autopause_broadcasts_referencing(
                conn,
                tenant_id=tenant_id,
                template_id=template_id,
                trigger_status=new_status,
            )

        # Outbound event row (Phase 6 persists; v3.0 dispatches). The
        # webhook_events_outbound table is created elsewhere; if it
        # doesn't exist yet (Phase 4 stub may have skipped it), the
        # INSERT will raise — we log + swallow so the lifecycle
        # status flip still completes. Defensive guard removed when
        # the outbound table is confirmed present in CI.
        try:
            await _persist_outbound_event(
                conn,
                tenant_id=tenant_id,
                event_type=f"template.{new_status}",
                payload={
                    "template_id": str(template_id),
                    "meta_template_id": meta_template_id,
                    "status": new_status,
                    "rejection_reason": reason,
                },
            )
        except asyncpg.UndefinedTableError:
            logger.warning(
                "template lifecycle: webhook_events_outbound missing; "
                "skipping outbound event persistence for tenant=%s template=%s",
                tenant_id,
                template_id,
            )

    return LifecycleResult(
        outcome="applied",
        tenant_id=tenant_id,
        template_id=template_id,
        new_status=new_status,
        broadcasts_paused=broadcasts_paused,
    )


async def _autopause_broadcasts_referencing(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    template_id: UUID,
    trigger_status: str,
) -> int:
    """Pause every scheduled / running broadcast on this template.

    Per Q-PHASE6-03: a broadcast worker still draining against a
    just-rejected template would 4xx-per-recipient. Pausing here
    prevents the burn; tenant can resume after re-submitting + Meta
    re-approving.

    The broadcasts row is flipped to ``status='paused'``; the worker
    checks broadcasts.status between recipients and bails. We also
    persist ``error.code='TEMPLATE_STATUS_CHANGED'`` for the portal
    to surface "your broadcast paused because template was rejected".

    Note: removal from the Redis ZSET happens in
    ``services/broadcast/service.pause_broadcast`` — at this point
    in Phase 6 we don't have access to that service from the
    lifecycle handler without circular imports. Per the deep-dive
    auto-pause cascade specification we accept the latency: worker
    sees status='paused' on its next tick and stops draining. The
    ZSET entry is harmless until the worker drains it on next pickup.
    """
    rows = await conn.fetch(
        """
        UPDATE broadcasts
           SET status = 'paused',
               error  = jsonb_build_object(
                          'code', 'TEMPLATE_STATUS_CHANGED',
                          'trigger_status', $3::text,
                          'paused_at', now()::text
                        ),
               updated_at = now()
         WHERE template_id = $1
           AND tenant_id   = $2
           AND status IN ('scheduled', 'running')
         RETURNING id
        """,
        template_id,
        tenant_id,
        trigger_status,
    )
    if rows:
        logger.info(
            "template lifecycle auto-pause: tenant=%s template=%s "
            "trigger=%s broadcasts_paused=%d",
            tenant_id,
            template_id,
            trigger_status,
            len(rows),
        )
    return len(rows)


async def _persist_outbound_event(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Persist one row into webhook_events_outbound (deferred dispatch).

    The table schema is owned by Phase 4 (the outbound worker stub).
    We INSERT the minimum the v3.0 worker will need: tenant_id, event
    type, JSONB payload, created_at.

    Wrapped in a try/UndefinedTableError at the call site because
    Phase 4's table may not exist in every test fixture.
    """
    await conn.execute(
        """
        INSERT INTO webhook_events_outbound
            (tenant_id, event_type, payload, created_at)
        VALUES ($1, $2, $3::jsonb, now())
        """,
        tenant_id,
        event_type,
        # dict DIRECTLY — the JSONB codec (pools.py) serialises once; json.dumps
        # would double-encode into a JSONB string (see services/audit.py).
        payload,
    )
