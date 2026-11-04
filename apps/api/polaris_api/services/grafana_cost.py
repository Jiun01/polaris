"""Grafana Cloud Mimir cost proxy — Phase 9 PR-10.

Reads per-tenant LLM/storage cost metrics from a Grafana Cloud Mimir
(Prometheus-compatible) HTTP API and returns TenantCostMonthly payloads.

Design decisions:
  - Seven PromQL queries per tenant (Anthropic input, Anthropic output,
    Anthropic cache-write, Anthropic cache-read, Cohere rerank, OpenRouter
    embedding, Supabase pass-through). Cache write/read are priced at
    Anthropic's prompt-cache rates (~1.25× / ~0.1× of input) and folded
    into ``anthropic_input_cost_myr``; embedding tokens become
    ``openrouter_embedding_cost_myr``.
  - 60-second in-memory cache per (tenant_id, yyyymm) — a simple dict
    with timestamps; no new dependencies.
  - All Mimir failures (4xx, 5xx, network) → zero-cost result with
    data_source='unavailable'; the router never raises, the frontend
    shows an empty state.
  - Provider prices are quoted in USD (matching vendor price pages) and
    converted to MYR through ONE tunable FX rate (``settings.usd_to_myr_rate``,
    env ``USD_TO_MYR_RATE``, default ~4.15). Quoting in USD means an exchange-
    rate move is a single number to bump, not a value baked into every constant.
    Blended USD list prices, per million tokens (Haiku-dominant fleet at GA):
      INPUT:  $1.00 / 1M    (uncached prompt)
      OUTPUT: $5.00 / 1M
      CACHE WRITE: $1.25 / 1M  (1.25× input — prompt-cache creation)
      CACHE READ:  $0.10 / 1M  (0.10× input — prompt-cache hit)
    We aggregate without per-model splitting at this layer (the PromQL sums all
    models together). The per-intent-tier breakdown reads raw token counts.
  - Cohere Rerank (Ultra): ~$0.002 per call.
  - OpenRouter embedding (qwen3-embedding-8b): ~$0.01 / 1M tokens.
  - Supabase: tracked as direct MYR spend from the
    ``polaris_supabase_cost_myr_total`` gauge (already in MYR, no FX applied).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import httpx
from polaris_shared_types.tenant_cost import (
    AlertBand,
    PerIntentTierBreakdown,
    TenantCostMonthly,
)

from polaris_api.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 60

# Provider list prices in USD per MILLION tokens (per-call for Cohere). These
# track the vendors' published USD prices; the ringgit figure on the dashboard
# is derived at query time via _usd_to_myr() so an FX move is one tunable knob.
_USD_PER_M_INPUT = Decimal("1.00")    # blended Haiku-heavy fleet, uncached input
_USD_PER_M_OUTPUT = Decimal("5.00")

# Anthropic prompt-cache multipliers, relative to the base input rate:
#   cache WRITE (cache_creation) ≈ 1.25× input  → 25% dearer to store
#   cache READ  (cache_read)     ≈ 0.10× input  → 10× cheaper than re-sending
# Disjoint from input_tokens (Anthropic reports all three), so the input cost
# sums base + write + read rather than discounting a single input figure.
_USD_PER_M_CACHE_WRITE = _USD_PER_M_INPUT * Decimal("1.25")  # $1.25/1M
_USD_PER_M_CACHE_READ = _USD_PER_M_INPUT * Decimal("0.10")   # $0.10/1M

# Cohere Rerank (Ultra): ~$0.002 per call.
_USD_PER_COHERE_CALL = Decimal("0.002")

# OpenRouter embedding (qwen3-embedding-8b): ~$0.01 / 1M tokens. Tiny per
# message — exists so the dashboard shows a measured figure, not a false zero.
_USD_PER_M_EMBEDDING = Decimal("0.01")

# FX fallback if the setting is missing/invalid. Provider prices above are USD;
# this converts to MYR. The authoritative value is settings.usd_to_myr_rate.
_DEFAULT_USD_TO_MYR = Decimal("4.15")


def _usd_to_myr() -> Decimal:
    """Current USD→MYR rate for cost conversion (single FX knob for the model).

    Reads ``settings.usd_to_myr_rate`` (env ``USD_TO_MYR_RATE``), falling back
    to ~4.15 if unset or non-positive. It's a manual estimate, not a live feed —
    ops bumps it when the rate drifts materially.
    """
    rate = getattr(get_settings(), "usd_to_myr_rate", None)
    try:
        d = Decimal(str(rate))
    except (InvalidOperation, TypeError, ValueError):
        return _DEFAULT_USD_TO_MYR
    return d if d > 0 else _DEFAULT_USD_TO_MYR

# ---------------------------------------------------------------------------
# Wrapper that carries data_source alongside the Pydantic model
# ---------------------------------------------------------------------------


@dataclass
class CostResult:
    """Thin wrapper returned by ``fetch_tenant_cost_monthly``.

    ``data_source`` is 'mimir' when data was retrieved successfully, or
    'unavailable' when Mimir was unreachable.  The wrapped ``cost``
    attribute is the serialisable ``TenantCostMonthly`` Pydantic model.
    Tests inspect ``.data_source``; the router reads ``.cost``.
    """

    cost: TenantCostMonthly
    data_source: str

    # Proxy attribute access to the inner model so callers can do
    # ``result.tenant_id``, ``result.total_cost_myr`` etc. transparently.
    def __getattr__(self, name: str) -> Any:
        return getattr(self.cost, name)


# ---------------------------------------------------------------------------
# In-memory cache: key = (tenant_id_str, yyyymm), value = (ts, result)
# ---------------------------------------------------------------------------

_COST_CACHE: dict[tuple[str, str], tuple[float, CostResult]] = {}


# ---------------------------------------------------------------------------
# Sentinel exception for HTTP errors
# ---------------------------------------------------------------------------


class MimirError(Exception):
    """Raised when Mimir returns a non-2xx HTTP status."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Mimir HTTP {status_code}")


# ---------------------------------------------------------------------------
# PromQL query builders
# ---------------------------------------------------------------------------


def _build_input_tokens_query(*, tenant_id: str) -> str:
    return (
        f'sum by (tenant_id, intent_tier, model_used) ('
        f'increase(polaris_anthropic_input_tokens_total'
        f'{{tenant_id="{tenant_id}"}}[30d])'
        f')'
    )


def _build_output_tokens_query(*, tenant_id: str) -> str:
    return (
        f'sum by (tenant_id, intent_tier, model_used) ('
        f'increase(polaris_anthropic_output_tokens_total'
        f'{{tenant_id="{tenant_id}"}}[30d])'
        f')'
    )


def _build_cache_write_tokens_query(*, tenant_id: str) -> str:
    return (
        f'sum by (tenant_id, intent_tier, model_used) ('
        f'increase(polaris_anthropic_cache_write_tokens_total'
        f'{{tenant_id="{tenant_id}"}}[30d])'
        f')'
    )


def _build_cache_read_tokens_query(*, tenant_id: str) -> str:
    return (
        f'sum by (tenant_id, intent_tier, model_used) ('
        f'increase(polaris_anthropic_cache_read_tokens_total'
        f'{{tenant_id="{tenant_id}"}}[30d])'
        f')'
    )


def _build_cohere_query(*, tenant_id: str) -> str:
    return (
        f'sum by (tenant_id) ('
        f'increase(polaris_cohere_rerank_calls_total'
        f'{{tenant_id="{tenant_id}"}}[30d])'
        f')'
    )


def _build_embedding_query(*, tenant_id: str) -> str:
    return (
        f'sum by (tenant_id) ('
        f'increase(polaris_openrouter_embedding_tokens_total'
        f'{{tenant_id="{tenant_id}"}}[30d])'
        f')'
    )


def _build_supabase_query(*, tenant_id: str) -> str:
    return (
        f'sum by (tenant_id) ('
        f'polaris_supabase_cost_myr_total'
        f'{{tenant_id="{tenant_id}"}}'
        f')'
    )


# ---------------------------------------------------------------------------
# Low-level HTTP call to Mimir query_range API
# ---------------------------------------------------------------------------


def _normalize_mimir_base(url: str) -> str:
    """Return the Prometheus *query* base for a Grafana Cloud / Mimir URL.

    Grafana Cloud exposes two paths under the same host:
      - remote-write (push):  ``{host}/api/prom/push``
      - query (read):         ``{host}/api/prom`` → ``.../api/prom/api/v1/query``

    ``GRAFANA_CLOUD_MIMIR_URL`` is meant to be the *query* base, but the
    remote-write ``.../api/prom/push`` URL is an easy copy-paste mistake — and
    appending ``/api/v1/query`` to it yields ``.../push/api/v1/query``, which
    Grafana returns **404** for (the symptom this guards against). Strip a
    trailing ``/push`` so a push URL still resolves to the query endpoint.
    """
    base = url.rstrip("/")
    if base.endswith("/push"):
        base = base[: -len("/push")]
    return base


async def _query_mimir(
    url: str,
    token: str,
    query: str,
    time_param: str | None,
) -> dict[str, Any]:
    """POST to Mimir /api/v1/query; returns the parsed JSON body.

    Raises MimirError on non-2xx. Raises httpx errors on network failure.
    """
    endpoint = _normalize_mimir_base(url) + "/api/v1/query"
    params: dict[str, str] = {"query": query}
    if time_param:
        params["time"] = time_param

    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.get(
            endpoint,
            params=params,
            headers={"Authorization": f"Basic {token}"},
        )

    if res.status_code >= 400:
        raise MimirError(res.status_code)

    return res.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Result parsing helpers
# ---------------------------------------------------------------------------


def _sum_values(mimir_result: dict[str, Any]) -> float:
    """Sum all numeric values from a Mimir query result vector."""
    total = 0.0
    try:
        results = mimir_result.get("data", {}).get("result", [])
        for item in results:
            value = item.get("value", [None, "0"])
            total += float(value[1])
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        logger.warning(
            "grafana_cost._sum_values: malformed Mimir result — cost total "
            "under-reported for this query. err=%s result_keys=%s",
            type(exc).__name__,
            list(mimir_result.keys()) if isinstance(mimir_result, dict) else None,
        )
    return total


def _extract_tier_breakdown(
    input_result: dict[str, Any],
    output_result: dict[str, Any],
    cache_read_result: dict[str, Any] | None = None,
) -> list[PerIntentTierBreakdown]:
    """Extract per-intent-tier stats from raw Mimir vectors."""
    # Build (tier, model) → {input, output, cache_read} map
    tier_map: dict[tuple[str, str], dict[str, float]] = {}

    def _accumulate(result: dict[str, Any], bucket: str) -> None:
        for item in result.get("data", {}).get("result", []):
            metric = item.get("metric", {})
            tier = metric.get("intent_tier", "unknown")
            model = metric.get("model_used", "unknown")
            key = (tier, model)
            if key not in tier_map:
                tier_map[key] = {"input": 0.0, "output": 0.0, "cache_read": 0.0}
            try:
                tier_map[key][bucket] += float(item["value"][1])
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                logger.warning(
                    "grafana_cost._accumulate: malformed Mimir item for "
                    "tier=%s model=%s bucket=%s — value dropped from cost "
                    "breakdown. err=%s",
                    tier, model, bucket, type(exc).__name__,
                )

    _accumulate(input_result, "input")
    _accumulate(output_result, "output")
    if cache_read_result is not None:
        _accumulate(cache_read_result, "cache_read")

    breakdown: list[PerIntentTierBreakdown] = []
    for (tier, model), counts in sorted(tier_map.items()):
        inp = counts["input"]
        out = counts["output"]
        cache_read = counts["cache_read"]
        # Hit rate = cache reads / total prompt tokens served (cached + uncached
        # input), per the PerIntentTierBreakdown field contract.
        prompt_total = inp + cache_read
        cache_hit_rate = 0.0 if prompt_total == 0 else cache_read / prompt_total
        breakdown.append(
            PerIntentTierBreakdown(
                intent_tier=tier,
                model_used=model,
                avg_input_tokens=int(inp),
                avg_output_tokens=int(out),
                avg_cache_read_tokens=int(cache_read),
                cache_hit_rate=round(cache_hit_rate, 4),
                sample_count=0,
            )
        )
    return breakdown


# ---------------------------------------------------------------------------
# Alert band + cost assembly
# ---------------------------------------------------------------------------


def _compute_alert_band(total_cost: Decimal, mrr: Decimal) -> AlertBand:
    """Compute alert band from cost-vs-MRR ratio.

    MRR=0 (unconfigured tenant) → green to avoid division-by-zero noise.
    Thresholds: >50% MRR = amber, >100% MRR = red.
    """
    if mrr <= Decimal("0"):
        return AlertBand.GREEN
    ratio = total_cost / mrr
    if ratio > Decimal("1"):
        return AlertBand.RED
    if ratio > Decimal("0.5"):
        return AlertBand.AMBER
    return AlertBand.GREEN


def _build_cost_result(
    *,
    tenant_id: UUID,
    yyyymm: str,
    anthropic_input_cost_myr: Decimal,
    anthropic_output_cost_myr: Decimal,
    cohere_rerank_cost_myr: Decimal,
    supabase_storage_cost_myr: Decimal,
    openrouter_embedding_cost_myr: Decimal,
    tenant_mrr_myr: Decimal,
    data_source: str,
    per_intent_tier: list[PerIntentTierBreakdown] | None = None,
) -> CostResult:
    """Assemble a CostResult wrapping TenantCostMonthly + data_source."""
    # VARIABLE cost (Polaris COGS) EXCLUDES Supabase — Supabase is fixed platform
    # overhead now tracked in platform_fixed_costs, not attributed per tenant.
    total = (
        anthropic_input_cost_myr
        + anthropic_output_cost_myr
        + cohere_rerank_cost_myr
        + openrouter_embedding_cost_myr
    )
    alert_band = _compute_alert_band(total, tenant_mrr_myr)
    pct = float(total / tenant_mrr_myr) if tenant_mrr_myr > 0 else 0.0
    margin = tenant_mrr_myr - total
    margin_pct = float(margin / tenant_mrr_myr) if tenant_mrr_myr > 0 else 0.0

    cost = TenantCostMonthly(
        tenant_id=tenant_id,
        period_yyyymm=yyyymm,
        anthropic_input_cost_myr=anthropic_input_cost_myr.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ),
        anthropic_output_cost_myr=anthropic_output_cost_myr.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ),
        cohere_rerank_cost_myr=cohere_rerank_cost_myr.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ),
        supabase_storage_cost_myr=supabase_storage_cost_myr.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ),
        openrouter_embedding_cost_myr=openrouter_embedding_cost_myr.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ),
        total_cost_myr=total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        tenant_mrr_myr=tenant_mrr_myr.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ),
        contribution_margin_myr=margin.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        contribution_margin_pct=round(margin_pct, 4),
        cost_pct_of_mrr=round(pct, 4),
        alert_band=alert_band,
        per_intent_tier=per_intent_tier or [],
    )
    return CostResult(cost=cost, data_source=data_source)


def _zero_cost(
    *,
    tenant_id: UUID,
    yyyymm: str,
    tenant_mrr_myr: Decimal,
) -> CostResult:
    """Return a zero-cost result with data_source='unavailable'."""
    return _build_cost_result(
        tenant_id=tenant_id,
        yyyymm=yyyymm,
        anthropic_input_cost_myr=Decimal("0"),
        anthropic_output_cost_myr=Decimal("0"),
        cohere_rerank_cost_myr=Decimal("0"),
        supabase_storage_cost_myr=Decimal("0"),
        openrouter_embedding_cost_myr=Decimal("0"),
        tenant_mrr_myr=tenant_mrr_myr,
        data_source="unavailable",
    )


# E2E cost stub — deterministic, month-aware fixture data (no Mimir).
# Band ratio per tenant keyed on the UUID's trailing byte so the all-tenant
# table is guaranteed to contain green + amber + red rows across the fleet.
_STUB_RATIO_BY_BUCKET = {
    0: Decimal("0.30"),  # green  (<50% MRR)
    1: Decimal("0.70"),  # amber  (>50% MRR)
    2: Decimal("1.30"),  # red    (>100% MRR)
}
_STUB_FALLBACK_MRR = Decimal("800")  # used when no tenant_billing.mrr_myr seeded


def _stub_cost(
    *,
    tenant_id: UUID,
    yyyymm: str,
    tenant_mrr_myr: Decimal,
    current_yyyymm: str,
    include_tier_breakdown: bool = False,
) -> CostResult:
    """Return deterministic synthetic cost data for E2E (``COST_STUB_ENABLED``).

    Past/current months yield non-zero, band-spread figures; future months
    resolve to ``unavailable`` so the dashboard's empty-state branch stays
    reachable. Never raises.
    """
    if yyyymm > current_yyyymm:
        # No cost has accrued for a future period — mirror Mimir's empty state.
        return _zero_cost(tenant_id=tenant_id, yyyymm=yyyymm, tenant_mrr_myr=tenant_mrr_myr)

    mrr = tenant_mrr_myr if tenant_mrr_myr > 0 else _STUB_FALLBACK_MRR
    bucket = int(str(tenant_id).replace("-", "")[-2:], 16) % 3
    total = mrr * _STUB_RATIO_BY_BUCKET[bucket]

    per_intent_tier: list[PerIntentTierBreakdown] = []
    if include_tier_breakdown:
        per_intent_tier = [
            PerIntentTierBreakdown(
                intent_tier="simple",
                model_used="claude-haiku-4-5",
                avg_input_tokens=1200,
                avg_output_tokens=300,
                avg_cache_read_tokens=0,
                cache_hit_rate=0.0,
                sample_count=42,
            ),
            PerIntentTierBreakdown(
                intent_tier="medium",
                model_used="claude-haiku-4-5",
                avg_input_tokens=2400,
                avg_output_tokens=600,
                avg_cache_read_tokens=0,
                cache_hit_rate=0.0,
                sample_count=18,
            ),
            PerIntentTierBreakdown(
                intent_tier="hard",
                model_used="claude-sonnet-5",
                avg_input_tokens=4800,
                avg_output_tokens=1200,
                avg_cache_read_tokens=0,
                cache_hit_rate=0.0,
                sample_count=7,
            ),
        ]

    return _build_cost_result(
        tenant_id=tenant_id,
        yyyymm=yyyymm,
        anthropic_input_cost_myr=total * Decimal("0.45"),
        anthropic_output_cost_myr=total * Decimal("0.40"),
        cohere_rerank_cost_myr=total * Decimal("0.10"),
        supabase_storage_cost_myr=mrr * Decimal("0.05"),
        openrouter_embedding_cost_myr=total * Decimal("0.05"),
        tenant_mrr_myr=mrr,
        data_source="stub",
        per_intent_tier=per_intent_tier,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def fetch_tenant_cost_monthly(
    *,
    tenant_id: UUID,
    yyyymm: str,
    tenant_mrr_myr: Decimal,
    mimir_url: str,
    mimir_token: str,
    include_tier_breakdown: bool = False,
) -> CostResult:
    """Fetch per-tenant monthly cost from Mimir with 60-second cache.

    Parameters
    ----------
    tenant_id:
        The tenant UUID to filter on.
    yyyymm:
        The month in YYYYMM format, e.g. '202606' for June 2026.
    tenant_mrr_myr:
        The tenant's MRR in MYR, read from tenant_billing.mrr_myr.
    mimir_url:
        GRAFANA_CLOUD_MIMIR_URL.
    mimir_token:
        GRAFANA_CLOUD_MIMIR_TOKEN (Basic auth token).
    include_tier_breakdown:
        If True, populates per_intent_tier. False for the all-tenants list.

    Returns
    -------
    TenantCostMonthly
        Always returns a valid object; zeros with data_source='unavailable'
        on any Mimir failure.
    """
    cache_key = (str(tenant_id), yyyymm)
    now = time.monotonic()

    if cache_key in _COST_CACHE:
        cached_ts, cached_result = _COST_CACHE[cache_key]
        if now - cached_ts < _CACHE_TTL_SECONDS:
            return cached_result

    tid_str = str(tenant_id)

    try:
        input_res = await _query_mimir(
            mimir_url, mimir_token, _build_input_tokens_query(tenant_id=tid_str), None
        )
        output_res = await _query_mimir(
            mimir_url, mimir_token, _build_output_tokens_query(tenant_id=tid_str), None
        )
        cache_write_res = await _query_mimir(
            mimir_url, mimir_token, _build_cache_write_tokens_query(tenant_id=tid_str), None
        )
        cache_read_res = await _query_mimir(
            mimir_url, mimir_token, _build_cache_read_tokens_query(tenant_id=tid_str), None
        )
        cohere_res = await _query_mimir(
            mimir_url, mimir_token, _build_cohere_query(tenant_id=tid_str), None
        )
        embedding_res = await _query_mimir(
            mimir_url, mimir_token, _build_embedding_query(tenant_id=tid_str), None
        )
        supabase_res = await _query_mimir(
            mimir_url, mimir_token, _build_supabase_query(tenant_id=tid_str), None
        )
    except Exception as exc:  # noqa: BLE001 — any failure → graceful zero result
        # Make the failure self-diagnosing: a 404 vs 401/403 point at different
        # misconfigurations (wrong URL path vs bad/insufficient credentials).
        hint = ""
        if isinstance(exc, MimirError):
            if exc.status_code == 404:  # noqa: PLR2004
                hint = (
                    " (404 — GRAFANA_CLOUD_MIMIR_URL must be the query base "
                    "'.../api/prom', NOT the '/push' remote-write URL)"
                )
            elif exc.status_code in (401, 403):
                hint = (
                    " (auth — GRAFANA_CLOUD_MIMIR_TOKEN must be a base64("
                    "instanceID:token) credential whose Cloud Access Policy "
                    "has the 'metrics:read' scope)"
                )
        logger.warning(
            "grafana_cost: Mimir unavailable for tenant=%s yyyymm=%s err=%s%s",
            tenant_id,
            yyyymm,
            exc,
            hint,
        )
        zero = _zero_cost(tenant_id=tenant_id, yyyymm=yyyymm, tenant_mrr_myr=tenant_mrr_myr)
        # Cache the zero result too to avoid hammering a down Mimir
        _COST_CACHE[cache_key] = (now, zero)
        return zero

    # Token counts → MYR costs
    input_tokens = _sum_values(input_res)
    output_tokens = _sum_values(output_res)
    cache_write_tokens = _sum_values(cache_write_res)
    cache_read_tokens = _sum_values(cache_read_res)
    cohere_calls = _sum_values(cohere_res)
    embedding_tokens = _sum_values(embedding_res)
    supabase_spend = _sum_values(supabase_res)

    # Convert USD list prices → MYR through the single FX knob.
    fx = _usd_to_myr()
    # Input cost sums the three disjoint Anthropic prompt-cache buckets:
    # uncached input (1×) + cache writes (1.25×) + cache reads (0.1×).
    anthropic_input_cost = (
        Decimal(str(input_tokens)) * _USD_PER_M_INPUT
        + Decimal(str(cache_write_tokens)) * _USD_PER_M_CACHE_WRITE
        + Decimal(str(cache_read_tokens)) * _USD_PER_M_CACHE_READ
    ) * fx / Decimal("1_000_000")
    anthropic_output_cost = Decimal(str(output_tokens)) * _USD_PER_M_OUTPUT * fx / Decimal("1_000_000")
    cohere_cost = Decimal(str(cohere_calls)) * _USD_PER_COHERE_CALL * fx
    embedding_cost = Decimal(str(embedding_tokens)) * _USD_PER_M_EMBEDDING * fx / Decimal("1_000_000")
    # Supabase gauge is already MYR — no FX applied.
    supabase_cost = Decimal(str(supabase_spend))

    tier_breakdown: list[PerIntentTierBreakdown] = []
    if include_tier_breakdown:
        tier_breakdown = _extract_tier_breakdown(input_res, output_res, cache_read_res)

    result = _build_cost_result(
        tenant_id=tenant_id,
        yyyymm=yyyymm,
        anthropic_input_cost_myr=anthropic_input_cost,
        anthropic_output_cost_myr=anthropic_output_cost,
        cohere_rerank_cost_myr=cohere_cost,
        supabase_storage_cost_myr=supabase_cost,
        openrouter_embedding_cost_myr=embedding_cost,
        tenant_mrr_myr=tenant_mrr_myr,
        data_source="mimir",
        per_intent_tier=tier_breakdown,
    )
    _COST_CACHE[cache_key] = (now, result)
    return result
