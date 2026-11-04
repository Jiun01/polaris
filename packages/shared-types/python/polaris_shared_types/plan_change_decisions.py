"""Plan-change decision shapes — Pydantic SSOT (Phase 9 PR-1; PR-8 read path).

Phase 8 created ``plan_change_requests`` (migration 051) and exposed
the tenant-side submit + read endpoints. Phase 9 PR-8 adds the
admin-side approval flow without further schema change: the admin
flips ``status`` pending → approved | rejected, writes
``decided_by_user_id`` + ``decided_at`` + ``decision_note``, and (on
approval) creates a ``tenant_invoices`` row with
``invoice_kind='plan_change_proration'``.

The shapes here are the admin-only request bodies — the tenant read
side stays on ``plan_change_requests.PlanChangeRequest``.

Anchors:
  - 00c §12.4 (plan-change request flow)
  - 00c §12.4.1 (proration mechanics — admin recomputes
    server-authoritatively on approval; the tenant's snapshot is a
    transparency artifact, not the source of truth)
  - 04j §6.1 (approval endpoint is idempotent — PCR.id is the
    idempotency key; a second approve call returns the existing
    invoice rather than creating a duplicate)
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")


class PlanChangeApprovalRequest(BaseModel):
    """Request body for ``POST /api/admin/plan-changes/{id}/approve``.

    The admin frontend supplies an optional decision_note (free-form
    text shown to the tenant) and an optional override of the
    server-recomputed proration. If proration_override is omitted, the
    server's authoritative recomputation is used. If supplied, the
    override is what lands on the issued invoice — must be used
    sparingly and with reason in decision_note.
    """

    model_config = _FORBID

    decision_note: str | None = Field(
        None,
        max_length=2000,
        description=(
            "Optional free-form note surfaced to the tenant in the "
            "approval notification + portal Billing screen."
        ),
    )
    proration_override: dict[str, object] | None = Field(
        None,
        description=(
            "Optional override of the server-recomputed proration "
            "(per 00c §12.4.1). If supplied, decision_note MUST explain why "
            "— typical use is a sales-led one-off adjustment."
        ),
    )


class PlanChangeRejectionRequest(BaseModel):
    """Request body for ``POST /api/admin/plan-changes/{id}/reject``.

    decision_note is required on rejection — the tenant sees the reason
    in their portal Billing screen.
    """

    model_config = _FORBID

    decision_note: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description=(
            "Required. Tenant sees this in their portal Billing screen "
            "+ the plan_change_rejected notification."
        ),
    )


__all__ = [
    "PlanChangeApprovalRequest",
    "PlanChangeRejectionRequest",
]
