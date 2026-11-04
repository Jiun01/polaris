"""Sales-quote (pre-conversion) shapes — Pydantic SSOT (Phase 9 PR-1).

Backing table: ``sales_quotes`` (migration 054).

The sales-quote tool is the front of the sales-led onboarding motion
(ADR-012). Operator uses it to pre-compute the prorated first invoice +
first-period quota for a prospect's intended signup date BEFORE the
prospect commits. The output is a one-page PDF with the two-tier
framing per 00c §12.4.2 ("Headline: Pro at RM 2,499/mo + 25k replies.
First invoice: prorated RM 886.65 + 8,871 replies for partial May.").

Anchors:
  - 00c §12.4.2 (sales-quote two-tier framing copy locked)
  - Q-PHASE9-05: PDF stored in Supabase Storage with byte-identical
    re-downloads; proration_inputs JSONB stored too so renderer-version
    bumps can diff against historical bytes
  - Q-PHASE9-04: WeasyPrint renderer (HTML + CSS templates iterate
    faster than ReportLab imperative layout at our 50–200 PDFs/month
    volume)
  - Reuses ``PlanTier`` + ``BillingCycle`` from plan_change_requests
    (no duplicate enum sprawl)
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from polaris_shared_types.plan_change_requests import BillingCycle, PlanTier

_FORBID = ConfigDict(extra="forbid")

# Regex mirrors the CHECK constraint in migration 054 (no-dash format —
# same convention as migration 008's invoice_number):
#   quote_code ~ '^POL-Q-[0-9]{2}[0-9]{2}[0-9]+$'
# Canonical example: 'POL-Q-26060042'.
QUOTE_CODE_PATTERN = r"^POL-Q-[0-9]{2}[0-9]{2}[0-9]+$"


class QuoteGenerateRequest(BaseModel):
    """Request body for ``POST /api/admin/quotes``.

    All other fields (quote_code, proration_inputs, pdf coordinates,
    expires_at) are server-computed.
    """

    model_config = _FORBID

    prospect_email: str | None = Field(None, max_length=254)
    prospect_company: str | None = Field(None, max_length=200)
    target_tier: PlanTier
    target_billing_cycle: BillingCycle
    intended_signup_date: str = Field(
        ...,
        description=(
            "ISO date YYYY-MM-DD. The prorated first invoice + quota are "
            "computed for the partial period from this date through the "
            "end of the calendar month (or anniversary cycle if requested "
            "via proration_basis)."
        ),
    )


class QuotePdfMeta(BaseModel):
    """Inline summary shown on the admin frontend before PDF render.

    Lets the operator confirm the numbers BEFORE the bytes are persisted
    to Supabase Storage. After PDF render this metadata is also serialised
    into ``sales_quotes.proration_inputs`` for byte-stability across
    re-downloads.
    """

    model_config = _FORBID

    headline_price_myr: Decimal = Field(
        ..., description="Full-period subscription price for target tier."
    )
    headline_reply_cap: int = Field(
        ..., ge=0, description="Full-period bot_replies cap for target tier."
    )
    first_invoice_myr: Decimal = Field(
        ..., description="Prorated charge for the partial first period."
    )
    first_period_reply_cap: int = Field(
        ...,
        ge=0,
        description="Prorated bot_replies cap for the partial first period.",
    )
    expires_at: str = Field(
        ...,
        description="RFC3339 UTC; default 30 days from generation.",
    )


class SalesQuote(BaseModel):
    """Read shape for ``GET /api/admin/quotes`` (and the per-id endpoint).

    The PDF download is a separate signed-URL redirect through
    ``GET /api/admin/quotes/{id}/pdf``; this shape carries the bucket
    coordinates only.
    """

    model_config = _FORBID

    id: UUID
    quote_code: str = Field(..., pattern=QUOTE_CODE_PATTERN)
    prospect_email: str | None
    prospect_company: str | None
    target_tier: PlanTier
    target_billing_cycle: BillingCycle
    intended_signup_date: str
    proration_inputs: dict[str, object] = Field(
        ...,
        description=(
            "Inputs for re-render: prorate_basis, days_remaining_in_period, "
            "headline_price, prorated_invoice, prorated_quota, "
            "renderer_version. Combined with the bucketed PDF bytes this "
            "gives byte-stable re-downloads per Q-PHASE9-05."
        ),
    )
    pdf_bucket: str
    pdf_object_key: str
    generated_by_admin_id: UUID
    converted_to_tenant_id: UUID | None = Field(
        None,
        description=(
            "Set on conversion via "
            "``POST /api/admin/quotes/{id}/convert/{tenant_id}``. NULL while "
            "the quote remains unconverted."
        ),
    )
    converted_at: str | None
    expires_at: str
    created_at: str


__all__ = [
    "QUOTE_CODE_PATTERN",
    "QuoteGenerateRequest",
    "QuotePdfMeta",
    "SalesQuote",
]
