"""In-house billing invoice shapes — Pydantic SSOT (Phase 9 PR-1).

Backing table: ``tenant_invoices`` (migration 008, ALTERed in migration 054).
Sequencer:    ``invoice_sequence`` (migration 008, composite PK
              (invoice_type, yy, mm) -> next_index). Phase 9 reuses this
              via the application-side INSERT...ON CONFLICT DO UPDATE
              pattern documented in 008.

Anchors:
  - ADR-028 (invoice format POL-{M|A}-YYMMINDEX; per-month monotonic;
    M=monthly, A=annual). Format has NO dash between yymm and index —
    canonical example: 'POL-M-26060042' (June 2026, 42nd issue).
  - 00c §12.4 / §12.4.1 (proration mechanics — proration_basis JSONB
    snapshots the inputs at issue time so a re-render is deterministic
    against historical bytes).
  - 04j §5.4 (WeasyPrint single-renderer module, used by both
    invoice issuance and sales-quote generation).
  - Migration 008 introduced this table in Phase 1; migration 054 adds
    Phase 9 columns (due_at, marked_paid_by_admin_id, void_reason,
    pdf_bucket, pdf_object_key, updated_at).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")

# Regex mirrors the CHECK constraint from migration 008 (NOT the dash form):
#   invoice_number ~ '^POL-[MA]-[0-9]{2}[0-9]{2}[0-9]+$'
# Format intent: POL-{M|A}-{yy}{mm}{index} with NO separator between
# yymm and index. Example: 'POL-M-26060042'.
INVOICE_NUMBER_PATTERN = r"^POL-[MA]-[0-9]{2}[0-9]{2}[0-9]+$"


class InvoiceStatus(StrEnum):
    OPEN    = "open"      # default per migration 008
    PAID    = "paid"
    OVERDUE = "overdue"
    VOID    = "void"


class IssueInvoiceRequest(BaseModel):
    """Request body for ``POST /api/admin/billing/invoices``.

    The router computes invoice_number via the invoice_sequence table,
    populates issued_at + status='open', and renders + uploads the PDF.
    period is a free-text label per migration 008 (e.g. '2026-06' for
    monthly, '2026' for annual); proration_basis JSONB carries the
    richer breakdown (kind, period_start/end, subtotal/tax split,
    quote reference, renderer_version) for audit.
    """

    model_config = _FORBID

    tenant_id: UUID
    period: str = Field(
        ...,
        min_length=4,
        max_length=20,
        description="Free-text period label. Convention: 'YYYY-MM' or 'YYYY'.",
    )
    amount_myr: Decimal = Field(
        ...,
        ge=0,
        decimal_places=2,
        description="Final charge to the tenant. Tax breakdown lives in proration_basis JSONB.",
    )
    due_at: str = Field(
        ...,
        description="RFC3339 UTC. Daily cron flips status -> overdue when NOW() > due_at AND status = 'open'.",
    )
    proration_basis: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "JSONB snapshot per 00c §12.4.1. Convention keys: "
            "{kind, period_start, period_end, subtotal_myr, tax_myr, "
            "original_tier, target_tier, prorate_basis, quote_id, "
            "renderer_version}. Audit-grade; do not mutate after issue."
        ),
    )


class TenantInvoice(BaseModel):
    """Read shape for ``GET /api/admin/billing/invoices`` (and the
    per-id endpoint).

    Columns from the merged 008 + 054 schema. Legacy ``pdf_url`` is kept
    so pre-Phase-9 rows continue to read; new code populates
    ``pdf_bucket`` + ``pdf_object_key`` and lets ``pdf_url`` stay NULL.
    """

    model_config = _FORBID

    id: UUID
    tenant_id: UUID
    invoice_number: str = Field(..., pattern=INVOICE_NUMBER_PATTERN)

    # Migration-008 columns
    period: str = Field(..., description="Free-text period label per migration 008.")
    amount_myr: Decimal = Field(..., ge=0)
    status: InvoiceStatus

    proration_basis: dict[str, object] | None = Field(
        None,
        description="JSONB snapshot of proration inputs per 00c §12.4.1. Nullable in legacy rows.",
    )
    issued_at: str = Field(..., description="RFC3339 UTC.")
    paid_at: str | None = Field(None, description="RFC3339 UTC; NULL until paid.")
    created_at: str

    # Legacy single URL (migration 008). New code uses pdf_bucket+pdf_object_key.
    pdf_url: str | None = None

    # Migration-054 columns (Phase 9 additions)
    due_at: str | None = Field(
        None,
        description="RFC3339 UTC. NULL on pre-Phase-9 rows; populated for all new issues.",
    )
    marked_paid_by_admin_id: UUID | None = Field(
        None,
        description="platform_admins.id who clicked Mark Paid. NULL on auto-reconciled or pre-Phase-9 rows.",
    )
    void_reason: str | None = None
    pdf_bucket: str | None = Field(
        None,
        description=(
            "Supabase Storage bucket name. Pairs with pdf_object_key. New rows "
            "use this; legacy rows keep pdf_url instead."
        ),
    )
    pdf_object_key: str | None = None
    updated_at: str | None = Field(
        None, description="RFC3339 UTC. NULL on pre-Phase-9 rows."
    )


__all__ = [
    "INVOICE_NUMBER_PATTERN",
    "InvoiceStatus",
    "IssueInvoiceRequest",
    "TenantInvoice",
]
