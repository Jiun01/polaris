"""Admin cost dashboard endpoints — Phase 9 PR-10.

Mounted at ``/api/admin/cost`` (registered in main.py).

Three read-only endpoints:

  * ``GET /tenants``                 — all-tenant monthly cost table with
                                       alert_band (green/amber/red).
  * ``GET /tenants/{tenant_id}``     — per-tenant detail with per-intent-tier
                                       breakdown (3 rows: simple/medium/hard).
  * ``GET /cohere-overrides``        — tenants with cohere_rerank_enabled IS
                                       NOT NULL; alert when Cohere > 5% MRR.

Auth: ``require_platform_admin`` from admin_tenants.py.
DB:   ``get_admin_service_conn`` (BYPASSRLS service pool).
Mimir: URL + token from Settings (GRAFANA_CLOUD_MIMIR_URL /
        GRAFANA_CLOUD_MIMIR_TOKEN). Mimir down → zeros, no 5xx to callers.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from polaris_shared_types.tenant_cost import (
    CohereOverrideCost,
    PlatformCostSummary,
    PlatformFixedCost,
    TenantCostMonthly,
)
from pydantic import BaseModel, ConfigDict, Field

from polaris_api.config import get_settings
from polaris_api.routers.admin_tenants import require_platform_admin
from polaris_api.services.admin import get_admin_service_conn
from polaris_api.services.grafana_cost import (
    CostResult,
    _stub_cost,
    _zero_cost,
    fetch_tenant_cost_monthly,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-cost"])
_FORBID = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------


class _AllTenantsResponse(BaseModel):
    model_config = _FORBID
    items: list[TenantCostMonthly]
    period_yyyymm: str
    data_source: str


class _CohereOverridesResponse(BaseModel):
    model_config = _FORBID
    items: list[CohereOverrideCost]
    period_yyyymm: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_yyyymm() -> str:
    return datetime.now(UTC).strftime("%Y%m")


def _parse_yyyymm(month: str | None) -> str:
    """Validate and return YYYYMM; default to current month."""
    if month is None:
        return _current_yyyymm()
    if len(month) != 6 or not month.isdigit():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="month must be in YYYYMM format, e.g. '202606'",
        )
    return month


async def _fetch_cost(
    *,
    tenant_id: UUID,
    yyyymm: str,
    mrr: Decimal,
    include_tier_breakdown: bool = False,
) -> CostResult:
    """Resolve Mimir credentials from Settings and fetch cost."""
    settings = get_settings()

    # E2E deterministic stub takes precedence over Mimir (which is absent locally).
    if getattr(settings, "cost_stub_enabled", False):
        return _stub_cost(
            tenant_id=tenant_id,
            yyyymm=yyyymm,
            tenant_mrr_myr=mrr,
            current_yyyymm=_current_yyyymm(),
            include_tier_breakdown=include_tier_breakdown,
        )

    mimir_url = getattr(settings, "grafana_cloud_mimir_url", "") or ""
    mimir_token = getattr(settings, "grafana_cloud_mimir_token", "") or ""

    if not mimir_url or not mimir_token:
        logger.warning("grafana_cost: GRAFANA_CLOUD_MIMIR_URL/TOKEN not configured — returning zeros")
        return _zero_cost(tenant_id=tenant_id, yyyymm=yyyymm, tenant_mrr_myr=mrr)

    return await fetch_tenant_cost_monthly(
        tenant_id=tenant_id,
        yyyymm=yyyymm,
        tenant_mrr_myr=mrr,
        mimir_url=mimir_url,
        mimir_token=mimir_token,
        include_tier_breakdown=include_tier_breakdown,
    )


async def _all_tenant_costs(
    conn: asyncpg.Connection, yyyymm: str
) -> tuple[list[TenantCostMonthly], str]:
    """All active tenants' monthly (variable) cost. Shared by the list + summary.

    Returns (items, data_source). Locally with no Grafana Mimir credentials each
    tenant resolves to zeros with data_source='unavailable' — NO fake fixtures.
    """
    rows = await conn.fetch(
        """
        SELECT t.id, t.display_name, COALESCE(tb.mrr_myr, 0) AS mrr_myr
        FROM tenants t
        LEFT JOIN tenant_billing tb ON tb.tenant_id = t.id
        WHERE t.status NOT IN ('deleted')
        ORDER BY t.display_name
        """,
    )
    items: list[TenantCostMonthly] = []
    data_source = "mimir"
    for row in rows:
        tenant_id = row["id"]
        tenant_name: str = row["display_name"] or str(tenant_id)
        mrr = Decimal(str(row["mrr_myr"]))
        result = await _fetch_cost(tenant_id=tenant_id, yyyymm=yyyymm, mrr=mrr)
        if result.data_source == "unavailable":
            data_source = "unavailable"
        items.append(result.cost.model_copy(update={"tenant_name": tenant_name}))
    return items, data_source


def _row_to_fixed(row: asyncpg.Record) -> PlatformFixedCost:
    return PlatformFixedCost(
        id=row["id"],
        label=row["label"],
        category=row["category"],
        monthly_myr=Decimal(str(row["monthly_myr"])),
        active=row["active"],
        notes=row["notes"],
    )


# ---------------------------------------------------------------------------
# GET /tenants — all-tenants monthly cost table
# ---------------------------------------------------------------------------


@router.get(
    "/tenants",
    response_model=_AllTenantsResponse,
    summary="All-tenant monthly cost table with amber/red banding",
)
async def list_tenant_costs(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
    month: str | None = Query(default=None, description="YYYYMM, e.g. 202606. Defaults to current month."),
) -> _AllTenantsResponse:
    """Return one TenantCostMonthly row per active tenant.

    MRR is read from ``tenant_billing.mrr_myr``. Mimir cost data is
    fetched concurrently (sequential at GA; parallel if perf matters
    post-GA). Mimir failures → zeros with data_source='unavailable'.
    """
    yyyymm = _parse_yyyymm(month)
    items, data_source = await _all_tenant_costs(conn, yyyymm)
    return _AllTenantsResponse(items=items, period_yyyymm=yyyymm, data_source=data_source)


# ---------------------------------------------------------------------------
# GET /tenants/{tenant_id} — per-tenant detail + intent-tier breakdown
# ---------------------------------------------------------------------------


@router.get(
    "/tenants/{tenant_id}",
    response_model=TenantCostMonthly,
    summary="Per-tenant cost detail with intent-tier breakdown",
)
async def get_tenant_cost(
    tenant_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
    month: str | None = Query(default=None, description="YYYYMM. Defaults to current month."),
) -> TenantCostMonthly:
    """Return per-intent-tier breakdown (simple / medium / hard) for one tenant."""
    yyyymm = _parse_yyyymm(month)

    # Verify tenant exists and fetch MRR
    row = await conn.fetchrow(
        """
        SELECT t.id, t.display_name, COALESCE(tb.mrr_myr, 0) AS mrr_myr
        FROM tenants t
        LEFT JOIN tenant_billing tb ON tb.tenant_id = t.id
        WHERE t.id = $1
        LIMIT 1
        """,
        tenant_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant_not_found")

    mrr = Decimal(str(row["mrr_myr"]))
    result = await _fetch_cost(
        tenant_id=tenant_id,
        yyyymm=yyyymm,
        mrr=mrr,
        include_tier_breakdown=True,
    )
    tenant_name: str = row["display_name"] or str(tenant_id)
    return result.cost.model_copy(update={"tenant_name": tenant_name})


# ---------------------------------------------------------------------------
# GET /cohere-overrides — tenants with cohere_rerank_enabled IS NOT NULL
# ---------------------------------------------------------------------------


@router.get(
    "/cohere-overrides",
    response_model=_CohereOverridesResponse,
    summary="Tenants with Cohere rerank override; alerts when cost > 5% MRR",
)
async def list_cohere_overrides(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
    month: str | None = Query(default=None, description="YYYYMM. Defaults to current month."),
) -> _CohereOverridesResponse:
    """Return CohereOverrideCost rows for tenants with a Cohere rerank override.

    Alert fires when Cohere cost alone exceeds 5% of that tenant's MRR.
    """
    yyyymm = _parse_yyyymm(month)

    rows = await conn.fetch(
        """
        SELECT t.id, t.display_name, COALESCE(tb.mrr_myr, 0) AS mrr_myr,
               t.cohere_rerank_enabled
        FROM tenants t
        LEFT JOIN tenant_billing tb ON tb.tenant_id = t.id
        WHERE t.cohere_rerank_enabled IS NOT NULL
          AND t.status NOT IN ('deleted')
        ORDER BY t.display_name
        """,
    )

    items: list[CohereOverrideCost] = []
    for row in rows:
        tenant_id = row["id"]
        mrr = Decimal(str(row["mrr_myr"]))
        result = await _fetch_cost(tenant_id=tenant_id, yyyymm=yyyymm, mrr=mrr)
        cohere_cost = result.cost.cohere_rerank_cost_myr
        pct = float(cohere_cost / mrr) if mrr > 0 else 0.0
        items.append(
            CohereOverrideCost(
                tenant_id=tenant_id,
                cohere_rerank_cost_myr=cohere_cost,
                tenant_mrr_myr=mrr.quantize(Decimal("0.01")),
                pct_of_mrr=round(pct, 4),
                alert=pct > 0.05,
            )
        )

    return _CohereOverridesResponse(items=items, period_yyyymm=yyyymm)


# ---------------------------------------------------------------------------
# GET /summary — platform P&L (fixed vs variable, kept separate)
# ---------------------------------------------------------------------------


@router.get(
    "/summary",
    response_model=PlatformCostSummary,
    summary="Platform P&L: Σ MRR − (Σ per-tenant variable + Σ fixed pool)",
)
async def platform_cost_summary(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
    month: str | None = Query(default=None, description="YYYYMM. Defaults to current month."),
) -> PlatformCostSummary:
    """Aggregate the month's platform economics.

    Variable = Σ per-tenant COGS (Anthropic + Cohere + OpenRouter). Fixed =
    Σ active platform_fixed_costs. Gross margin keeps the two separate — fixed
    cost is NOT amortized into per-tenant figures.
    """
    yyyymm = _parse_yyyymm(month)
    items, _ = await _all_tenant_costs(conn, yyyymm)
    total_variable = sum((i.total_cost_myr for i in items), Decimal("0"))
    total_mrr = sum((i.tenant_mrr_myr for i in items), Decimal("0"))

    fixed_rows = await conn.fetch(
        "SELECT id, label, category, monthly_myr, active, notes "
        "FROM platform_fixed_costs WHERE active = TRUE ORDER BY category NULLS LAST, label"
    )
    fixed_items = [_row_to_fixed(r) for r in fixed_rows]
    total_fixed = sum((f.monthly_myr for f in fixed_items), Decimal("0"))

    gross_margin = total_mrr - total_variable - total_fixed
    gross_margin_pct = float(gross_margin / total_mrr) if total_mrr > 0 else 0.0
    q2 = Decimal("0.01")
    return PlatformCostSummary(
        period_yyyymm=yyyymm,
        active_tenant_count=len(items),
        total_mrr_myr=total_mrr.quantize(q2),
        total_variable_cost_myr=total_variable.quantize(q2),
        total_fixed_cost_myr=total_fixed.quantize(q2),
        gross_margin_myr=gross_margin.quantize(q2),
        gross_margin_pct=round(gross_margin_pct, 4),
        fixed_costs=fixed_items,
    )


# ---------------------------------------------------------------------------
# Fixed-cost pool CRUD (operator-entered)
# ---------------------------------------------------------------------------


class _FixedCostsResponse(BaseModel):
    model_config = _FORBID
    items: list[PlatformFixedCost]


class _FixedCostCreate(BaseModel):
    model_config = _FORBID
    label: str = Field(..., min_length=1, max_length=120)
    category: str | None = Field(None, max_length=40)
    monthly_myr: Decimal = Field(..., ge=0)
    active: bool = True
    notes: str | None = Field(None, max_length=500)


class _FixedCostUpdate(BaseModel):
    """Partial update — only provided fields change."""

    model_config = _FORBID
    label: str | None = Field(None, min_length=1, max_length=120)
    category: str | None = Field(None, max_length=40)
    monthly_myr: Decimal | None = Field(None, ge=0)
    active: bool | None = None
    notes: str | None = Field(None, max_length=500)


@router.get(
    "/fixed",
    response_model=_FixedCostsResponse,
    summary="List platform fixed-cost line items (incl. inactive)",
)
async def list_fixed_costs(
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _FixedCostsResponse:
    rows = await conn.fetch(
        "SELECT id, label, category, monthly_myr, active, notes "
        "FROM platform_fixed_costs ORDER BY active DESC, category NULLS LAST, label"
    )
    return _FixedCostsResponse(items=[_row_to_fixed(r) for r in rows])


@router.post(
    "/fixed",
    response_model=PlatformFixedCost,
    status_code=status.HTTP_201_CREATED,
    summary="Add a fixed-cost line item",
)
async def create_fixed_cost(
    body: _FixedCostCreate,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> PlatformFixedCost:
    row = await conn.fetchrow(
        "INSERT INTO platform_fixed_costs (label, category, monthly_myr, active, notes) "
        "VALUES ($1, $2, $3, $4, $5) "
        "RETURNING id, label, category, monthly_myr, active, notes",
        body.label,
        body.category,
        body.monthly_myr,
        body.active,
        body.notes,
    )
    assert row is not None  # noqa: S101
    return _row_to_fixed(row)


@router.patch(
    "/fixed/{cost_id}",
    response_model=PlatformFixedCost,
    summary="Update a fixed-cost line item (partial)",
)
async def update_fixed_cost(
    cost_id: int,
    body: _FixedCostUpdate,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> PlatformFixedCost:
    # COALESCE keeps existing values for omitted fields.
    row = await conn.fetchrow(
        "UPDATE platform_fixed_costs SET "
        "  label = COALESCE($2, label), "
        "  category = COALESCE($3, category), "
        "  monthly_myr = COALESCE($4, monthly_myr), "
        "  active = COALESCE($5, active), "
        "  notes = COALESCE($6, notes), "
        "  updated_at = now() "
        "WHERE id = $1 "
        "RETURNING id, label, category, monthly_myr, active, notes",
        cost_id,
        body.label,
        body.category,
        body.monthly_myr,
        body.active,
        body.notes,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="fixed_cost_not_found")
    return _row_to_fixed(row)


@router.delete(
    "/fixed/{cost_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a fixed-cost line item",
)
async def delete_fixed_cost(
    cost_id: int,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    _admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> Response:
    result = await conn.execute("DELETE FROM platform_fixed_costs WHERE id = $1", cost_id)
    if result.endswith("0"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="fixed_cost_not_found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
