"""Per-tenant cost dashboard shapes — Pydantic SSOT (Phase 9 PR-1; PR-10 read path).

Sourced from Grafana Cloud Mimir, aggregated by FastAPI per
Q-PHASE9-06. The admin frontend calls /api/admin/cost/* and never
sees the GRAFANA_CLOUD_MIMIR_TOKEN directly — the token lives only in
apps/api/.env. FastAPI caches the response for 60 s to respect Mimir
rate limits.

Anchors:
  - 04 Phase 9 §9 (per-tenant cost dashboard reading Grafana Cloud OTel —
    Anthropic input/output, Cohere rerank, Supabase storage, OpenRouter
    embedding; amber > 50% MRR / red > 100% MRR)
  - 04 Phase 9 §9.1 (per-intent-tier breakdown — avg input / output /
    cache-read tokens + cache hit rate per haiku_simple / haiku_medium /
    sonnet_hard; cost-cutting revisit gate after 4 weeks of GA data)
  - 09 ADR-026 Am.5 (Cohere rerank Ultra+ only; the Cohere-override
    subpanel reads tenants.cohere_rerank_enabled per [00c §12.2])
  - 09 ADR-031 (intent classifier — values reuse llm_response.IntentTier
    Literal: 'simple' | 'medium' | 'hard' | 'handoff'; 'handoff' is
    excluded from the breakdown since it bypasses the LLM)
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")


class AlertBand(StrEnum):
    """Variable-cost-vs-MRR row coloring on the cost dashboard.

    Banded on per-tenant VARIABLE cost (Polaris COGS: Anthropic + Cohere +
    OpenRouter) vs MRR — the true contribution margin. Fixed platform overhead
    (Supabase/Redis/VPS/etc.) is tracked separately and is NOT in this ratio.
    """

    GREEN = "green"   # variable cost <= 50% of tenant's MRR (margin >= 50%)
    AMBER = "amber"   # 50% < variable cost <= 100%
    RED   = "red"     # variable cost > 100% — tenant is gross-margin negative


class PerIntentTierBreakdown(BaseModel):
    """One row of the per-intent-tier table on the per-tenant cost page.

    intent_tier mirrors ``polaris_shared_types.llm_response.IntentTier``
    values (simple | medium | hard); the 'handoff' Literal value is
    excluded because handoffs bypass the LLM entirely. Kept as plain
    string here (not the Literal) because the dashboard surface is
    label-driven and we may add tiers post-GA.
    """

    model_config = _FORBID

    intent_tier: str = Field(..., description="simple | medium | hard")
    model_used: str = Field(
        ...,
        description=(
            "Resolved model per ADR-031 routing — e.g. 'haiku' or 'sonnet'. "
            "Mirrors ``messages.model_used`` values."
        ),
    )
    avg_input_tokens: int = Field(..., ge=0)
    avg_output_tokens: int = Field(..., ge=0)
    avg_cache_read_tokens: int = Field(
        ...,
        ge=0,
        description=(
            "Non-zero indicates prompt cache is hitting; supports the "
            "Phase 4 exit-criterion-1 verification."
        ),
    )
    cache_hit_rate: float = Field(
        ...,
        ge=0,
        le=1,
        description=(
            "cache_read_tokens / (cache_read_tokens + input_tokens) over "
            "the sample. Surfaces in the cost-cutting revisit banner if "
            "< 30% after 4 weeks (signals system prompt churning or TTL "
            "too short)."
        ),
    )
    sample_count: int = Field(
        ...,
        ge=0,
        description="Number of LLM calls in the window.",
    )


class TenantCostMonthly(BaseModel):
    """One row of the all-tenants cost table + the per-tenant detail.

    period_yyyymm is the calendar month the costs were aggregated over;
    cost_pct_of_mrr drives alert_band. Per-intent-tier list is empty on
    the all-tenants list endpoint (omitted server-side to keep the
    payload light) and populated on the per-tenant detail endpoint.
    """

    model_config = _FORBID

    tenant_id: UUID
    tenant_name: str = Field(default="", description="Display name of the tenant for UI labelling.")
    period_yyyymm: str = Field(
        ..., pattern=r"^\d{6}$", description="YYYYMM, e.g. '202606' for June 2026."
    )
    anthropic_input_cost_myr: Decimal = Field(..., ge=0)
    anthropic_output_cost_myr: Decimal = Field(..., ge=0)
    cohere_rerank_cost_myr: Decimal = Field(..., ge=0)
    supabase_storage_cost_myr: Decimal = Field(
        ...,
        ge=0,
        description=(
            "Informational only — Supabase is now a FIXED platform cost "
            "(see PlatformCostSummary), NOT part of per-tenant variable cost. "
            "Retained for transparency / migration of the external Mimir gauge."
        ),
    )
    openrouter_embedding_cost_myr: Decimal = Field(..., ge=0)
    total_cost_myr: Decimal = Field(
        ...,
        ge=0,
        description=(
            "Per-tenant VARIABLE cost (Polaris COGS) = Anthropic in/out + "
            "Cohere + OpenRouter. EXCLUDES Supabase (now fixed). This is the "
            "marginal cost of serving the tenant."
        ),
    )
    tenant_mrr_myr: Decimal = Field(..., ge=0)
    contribution_margin_myr: Decimal = Field(
        ...,
        description="tenant_mrr_myr - total_cost_myr (variable). May be negative.",
    )
    contribution_margin_pct: float = Field(
        ...,
        description="contribution_margin_myr / tenant_mrr_myr (0–1; negative if upside-down).",
    )
    cost_pct_of_mrr: float = Field(
        ...,
        ge=0,
        description=(
            "variable total_cost_myr / tenant_mrr_myr, expressed as 0–N (not "
            "0–1) — 0.5 = 50%. Above 1.0 means the tenant's variable cost "
            "alone exceeds what they pay."
        ),
    )
    alert_band: AlertBand
    per_intent_tier: list[PerIntentTierBreakdown] = Field(
        default_factory=list,
        description=(
            "Empty on the all-tenants list endpoint (omitted server-side "
            "for payload size); populated on per-tenant detail."
        ),
    )


class CohereOverrideCost(BaseModel):
    """Row of the Cohere-Override subpanel.

    Lists only tenants where ``tenants.cohere_rerank_enabled IS NOT NULL`` —
    these are tenants with a per-tenant override of the Ultra+ tier gate
    (ADR-026 Am.5). Operator alert fires when Cohere cost per overridden
    tenant > 5% of their MRR (04 Phase 9 §9 spec).
    """

    model_config = _FORBID

    tenant_id: UUID
    cohere_rerank_cost_myr: Decimal = Field(..., ge=0)
    tenant_mrr_myr: Decimal = Field(..., ge=0)
    pct_of_mrr: float = Field(
        ...,
        ge=0,
        description="cohere_rerank_cost_myr / tenant_mrr_myr (0–N, not 0–1).",
    )
    alert: bool = Field(
        ...,
        description=(
            "True when pct_of_mrr > 0.05 (i.e. Cohere alone > 5% MRR). "
            "Surfaces as an amber row in the subpanel; operator reviews "
            "monthly."
        ),
    )


class PlatformFixedCost(BaseModel):
    """One operator-entered fixed/global platform cost line item.

    Fixed costs are NOT measured per tenant (Supabase plan, Redis, VPS,
    Grafana, Sentry, domains, …). The operator maintains these line items in
    the admin Cost page; the monthly total feeds the platform P&L. Persisted
    in the ``platform_fixed_costs`` table (migration 086).
    """

    model_config = _FORBID

    id: int
    label: str = Field(..., min_length=1, max_length=120)
    category: str | None = Field(
        None, max_length=40, description="Optional grouping, e.g. infra | observability."
    )
    monthly_myr: Decimal = Field(..., ge=0)
    active: bool = Field(default=True, description="Inactive line items are excluded from totals.")
    notes: str | None = Field(None, max_length=500)


class PlatformCostSummary(BaseModel):
    """Platform P&L for a month — fixed vs variable kept separate (no amortization).

    gross_margin = Σ MRR − (Σ per-tenant variable cost + Σ active fixed costs).
    """

    model_config = _FORBID

    period_yyyymm: str = Field(..., pattern=r"^\d{6}$")
    active_tenant_count: int = Field(..., ge=0)
    total_mrr_myr: Decimal = Field(..., ge=0)
    total_variable_cost_myr: Decimal = Field(
        ..., ge=0, description="Σ per-tenant variable (Anthropic + Cohere + OpenRouter)."
    )
    total_fixed_cost_myr: Decimal = Field(
        ..., ge=0, description="Σ active platform_fixed_costs line items."
    )
    gross_margin_myr: Decimal = Field(
        ..., description="total_mrr − total_variable − total_fixed. May be negative."
    )
    gross_margin_pct: float = Field(
        ..., description="gross_margin_myr / total_mrr_myr (0–1; negative if loss-making)."
    )
    fixed_costs: list[PlatformFixedCost] = Field(
        default_factory=list, description="Active fixed-cost line items behind total_fixed_cost_myr."
    )


__all__ = [
    "AlertBand",
    "CohereOverrideCost",
    "PerIntentTierBreakdown",
    "PlatformCostSummary",
    "PlatformFixedCost",
    "TenantCostMonthly",
]
