"""Tenant-status branching for resolved inbound webhooks — Phase 2 PR-3.

After the resolver binds a delivery to a ``tenants`` row, this gate
inspects ``(status, suspended_reason)`` and tells the dispatcher
whether to proceed, route to the Chatwoot human path, or drop silently.

Branch table (locked by ADR-018 + ADR-049 Amendment 3rd):

  status='active'                                 → PROCEED
  status='suspended' AND suspended_reason='quota_exhausted'
      → ROUTE_CHATWOOT_HUMAN  (no LLM emit; Chatwoot human path stays
                              active; the pol_bot_suspended label is
                              applied by the Phase 7 one-shot transition
                              handler, NOT per message here)
  status='suspended' AND suspended_reason='payment_overdue'
      → DROP_SILENTLY         (Chatwoot OFF per ADR-049 Am 3rd; ack 200
                              to Meta but no DB writes beyond the
                              webhook_events suspended_payment row)
  status='deleted'                                → DROP_DELETED
                              (soft-delete window per ADR-029; treat
                              like payment_overdue from the dispatcher's
                              perspective)
  any other shape                                 → DROP_DELETED
                              (defensive — unknown statuses are treated
                              as inactive)

This module is pure-data + pure-decision. The actual ``tenants`` row
lookup lives in ``services/webhook_resolver.fetch_tenant_status`` so
the BYPASSRLS pool import stays confined to one allowlisted file.
"""

from __future__ import annotations

from enum import StrEnum


class StatusDecision(StrEnum):
    """Outcome of the tenant-status check.

    ``StrEnum`` (Py 3.11+) lets log lines + JSON-serialisable audit
    rows use the value without an extra ``.value`` deref. The ordering
    of enum members mirrors the branch table above so readers can
    follow top-to-bottom.
    """

    PROCEED = "proceed"
    ROUTE_CHATWOOT_HUMAN = "route_chatwoot_human"
    DROP_SILENTLY = "drop_silently"
    DROP_DELETED = "drop_deleted"


def decide(status: str | None, suspended_reason: str | None) -> StatusDecision:
    """Map ``(status, suspended_reason)`` to a ``StatusDecision``.

    Pure function. Never raises. Unknown / null status falls to
    ``DROP_DELETED`` (fail closed — an unrecognised status shouldn't
    let the LLM pipeline fire on it).

    Parameters intentionally accept ``str`` (not the Postgres ``tenant_status``
    enum), so callers that pull the columns as plain text don't need a
    cast step. ``suspended_reason`` is the CHECK-constrained text column
    on ``tenants`` (one of 'quota_exhausted' / 'payment_overdue' / NULL).
    """
    if status == "active":
        return StatusDecision.PROCEED
    if status == "suspended":
        if suspended_reason == "quota_exhausted":
            return StatusDecision.ROUTE_CHATWOOT_HUMAN
        if suspended_reason == "payment_overdue":
            return StatusDecision.DROP_SILENTLY
        # suspended with an unrecognised / NULL reason: fail closed.
        return StatusDecision.DROP_DELETED
    if status == "deleted":
        return StatusDecision.DROP_DELETED
    return StatusDecision.DROP_DELETED


# Mapping from StatusDecision → webhook_events.status text. The decision
# enum is dispatcher-facing; this mapping is what lands in the
# persistent forensic row. Kept here so the two stay in lock-step.
DECISION_TO_EVENT_STATUS: dict[StatusDecision, str] = {
    StatusDecision.PROCEED: "received",
    StatusDecision.ROUTE_CHATWOOT_HUMAN: "suspended_quota",
    StatusDecision.DROP_SILENTLY: "suspended_payment",
    StatusDecision.DROP_DELETED: "suspended_deleted",
}
