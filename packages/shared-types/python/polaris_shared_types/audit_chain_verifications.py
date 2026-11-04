"""Audit hash-chain verification result — Pydantic SSOT (Phase 9 PR-1).

Backing table: ``audit_chain_verifications`` (migration 054).

Phase 1 shipped the audit_logs hash chain (ADR-030 §"audit trail
integrity"): each row stores prev_hash + row_hash so any in-place edit
or delete breaks the chain at the affected row and every subsequent
row. The verifier (services/audit.py::verify_chain, also from Phase 1)
walks (created_at, id) ASC and asserts BOTH the chain link AND the
row content.

Phase 9's contribution is **persistence + a viewer**:

  - A nightly APScheduler job (services/audit_chain_verifier.py +
    scheduled/audit_chain_verify.py, PR-5) runs verify_chain at 04:00
    Asia/Kuala_Lumpur, writes a row here with status='intact' /
    'broken' / 'errored'.
  - The admin Audit page reads the latest row and surfaces a status
    banner (green/red/grey).
  - An on-demand "Re-verify now" button (PR-5) triggers the same
    function with triggered_by='on_demand' for after-incident forensics.

Anchors:
  - Q-PHASE9-03 (nightly + on-demand instead of verify-on-every-read;
    a full chain scan can spike CPU on a 10M-row table — off-peak
    cadence is sufficient because tampering is rare and a 24-hour
    detection window is acceptable)
  - 04 Phase 9 §4 (audit log viewer with hash-chain verification on read)
  - 09 ADR-030 §"audit trail integrity" (the chain mechanics this
    verifier walks)
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")


class VerificationStatus(StrEnum):
    INTACT  = "intact"
    BROKEN  = "broken"
    ERRORED = "errored"   # verifier itself failed (DB error, OOM, etc.)


class TriggeredBy(StrEnum):
    SCHEDULED = "scheduled"   # nightly 04:00 MYT cron
    ON_DEMAND = "on_demand"   # admin button click — used for after-incident forensics


class AuditChainVerification(BaseModel):
    """Read shape for ``GET /api/admin/audit/verifications/latest`` and
    list endpoints.

    Broken-row fields (broken_at_id / broken_at_created_at / broken_*_hash)
    are NULL when status='intact'. When status='broken' they identify the
    first row where the chain fails — start the investigation tree there
    per ../docs/internal-ops/RUNBOOK.md "audit-chain break response".
    """

    model_config = _FORBID

    id: UUID
    checked_at: str = Field(..., description="RFC3339 UTC.")
    status: VerificationStatus
    rows_scanned: int = Field(
        ..., ge=0, description="Total audit_logs rows walked in this run."
    )
    duration_ms: int = Field(..., ge=0)

    broken_at_id: UUID | None = Field(
        None,
        description=(
            "audit_logs.id where the chain first broke. NULL when intact."
        ),
    )
    broken_at_created_at: str | None = Field(
        None,
        description=(
            "RFC3339 UTC of the broken row — useful for narrowing the "
            "investigation window."
        ),
    )
    broken_prev_hash_hex: str | None = Field(
        None,
        description=(
            "Hex-encoded BYTEA (lowercase, no 0x prefix) of the expected "
            "prev_hash (recomputed from the prior row). For "
            "transport-friendliness — the raw BYTEA is not JSON-safe."
        ),
    )
    broken_actual_hash_hex: str | None = Field(
        None,
        description=(
            "Hex-encoded BYTEA of the row_hash found on the broken row "
            "(what the chain stored)."
        ),
    )

    triggered_by: TriggeredBy
    triggered_by_admin_id: UUID | None = Field(
        None,
        description=(
            "Set when triggered_by='on_demand'. NULL for scheduled runs."
        ),
    )

    notes: str | None = None


__all__ = [
    "AuditChainVerification",
    "TriggeredBy",
    "VerificationStatus",
]
