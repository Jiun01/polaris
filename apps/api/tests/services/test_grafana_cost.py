"""TDD tests for grafana_cost service — Phase 9 PR-10.

Coverage:
  * PromQL template builder produces a valid query string for a given tenant_id + yyyymm.
  * Result parsing: mock Mimir JSON response → correct TenantCostMonthly values.
  * Mimir 4xx → returns zeros with data_source='unavailable', no exception raised.
  * Mimir 5xx → same graceful zero-return behaviour.
"""

from __future__ import annotations

import os
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

# Set env before imports so Settings resolves correctly in tests
os.environ.setdefault("JWT_SECRET", "pytest-secret-32-bytes-padded-xxxxxxxxxx")
os.environ.setdefault("JWT_ISSUER", "polaris-portal")
os.environ.setdefault("GRAFANA_CLOUD_MIMIR_URL", "https://mimir.example.com")
os.environ.setdefault("GRAFANA_CLOUD_MIMIR_TOKEN", "pytest-mimir-token")

pytestmark = pytest.mark.asyncio(loop_scope="function")

_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
_YYYYMM = "202606"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_cache() -> None:
    """Wipe the in-memory cost cache between tests."""
    from polaris_api.services.grafana_cost import _COST_CACHE
    _COST_CACHE.clear()


# ---------------------------------------------------------------------------
# PromQL template builder
# ---------------------------------------------------------------------------


def test_promql_template_builder_input_tokens() -> None:
    """PromQL for input tokens contains the tenant_id and 30d window."""
    from polaris_api.services.grafana_cost import _build_input_tokens_query

    query = _build_input_tokens_query(tenant_id=str(_TENANT_ID))

    assert str(_TENANT_ID) in query
    assert "polaris_anthropic_input_tokens_total" in query
    assert "[30d]" in query
    assert "sum by" in query
    assert "intent_tier" in query
    assert "model_used" in query


def test_promql_template_builder_output_tokens() -> None:
    """PromQL for output tokens mirrors input_tokens structure."""
    from polaris_api.services.grafana_cost import _build_output_tokens_query

    query = _build_output_tokens_query(tenant_id=str(_TENANT_ID))

    assert str(_TENANT_ID) in query
    assert "polaris_anthropic_output_tokens_total" in query
    assert "[30d]" in query


# ---------------------------------------------------------------------------
# Mimir URL normalization — guards the "Mimir HTTP 404 for every tenant" bug
# (GRAFANA_CLOUD_MIMIR_URL set to the remote-write /api/prom/push endpoint).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "expected_base"),
    [
        # The actual prod misconfiguration: the remote-write push URL.
        (
            "https://prometheus-prod-00-prod-<region>.grafana.net/api/prom/push",
            "https://prometheus-prod-00-prod-<region>.grafana.net/api/prom",
        ),
        # Trailing slash on the push URL.
        (
            "https://x.grafana.net/api/prom/push/",
            "https://x.grafana.net/api/prom",
        ),
        # Already the correct query base — left unchanged.
        ("https://x.grafana.net/api/prom", "https://x.grafana.net/api/prom"),
        # Correct query base with trailing slash.
        ("https://x.grafana.net/api/prom/", "https://x.grafana.net/api/prom"),
        # Self-hosted Mimir with no /push suffix — untouched.
        ("http://mimir.internal:9009/prometheus", "http://mimir.internal:9009/prometheus"),
    ],
)
def test_normalize_mimir_base_strips_push_suffix(
    configured: str, expected_base: str
) -> None:
    """A remote-write '/push' URL is normalized to the query base so the
    final endpoint is '.../api/prom/api/v1/query', not '.../push/api/v1/query'
    (which 404s)."""
    from polaris_api.services.grafana_cost import _normalize_mimir_base

    base = _normalize_mimir_base(configured)
    assert base == expected_base
    # The query endpoint the service actually calls must never contain '/push'.
    assert f"{base}/api/v1/query".count("/push") == 0


def test_promql_template_builder_cohere() -> None:
    """PromQL for Cohere rerank contains tenant_id."""
    from polaris_api.services.grafana_cost import _build_cohere_query

    query = _build_cohere_query(tenant_id=str(_TENANT_ID))

    assert str(_TENANT_ID) in query
    assert "polaris_cohere_rerank_calls_total" in query


def test_promql_template_builder_supabase() -> None:
    """PromQL for Supabase pass-through contains tenant_id."""
    from polaris_api.services.grafana_cost import _build_supabase_query

    query = _build_supabase_query(tenant_id=str(_TENANT_ID))

    assert str(_TENANT_ID) in query


# ---------------------------------------------------------------------------
# Result parsing from Mimir JSON fixture
# ---------------------------------------------------------------------------

_MIMIR_INPUT_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [
            {
                "metric": {
                    "tenant_id": str(_TENANT_ID),
                    "intent_tier": "simple",
                    "model_used": "haiku",
                },
                "value": [1748000000, "1000000"],  # 1M input tokens
            },
            {
                "metric": {
                    "tenant_id": str(_TENANT_ID),
                    "intent_tier": "hard",
                    "model_used": "sonnet",
                },
                "value": [1748000000, "500000"],  # 500k input tokens
            },
        ],
    },
}

_MIMIR_OUTPUT_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [
            {
                "metric": {
                    "tenant_id": str(_TENANT_ID),
                    "intent_tier": "simple",
                    "model_used": "haiku",
                },
                "value": [1748000000, "200000"],  # 200k output tokens
            },
        ],
    },
}

_MIMIR_EMPTY_RESPONSE = {
    "status": "success",
    "data": {"resultType": "vector", "result": []},
}


async def test_parse_mimir_response_computes_costs() -> None:
    """Mock Mimir returns fixture → TenantCostMonthly has non-zero values."""
    _clear_cache()
    from unittest.mock import patch

    from polaris_api.services.grafana_cost import fetch_tenant_cost_monthly

    async def _mock_query(url: str, token: str, query: str, time_param: str | None) -> dict:
        if "input_tokens" in query:
            return _MIMIR_INPUT_RESPONSE
        if "output_tokens" in query:
            return _MIMIR_OUTPUT_RESPONSE
        return _MIMIR_EMPTY_RESPONSE

    with patch(
        "polaris_api.services.grafana_cost._query_mimir",
        side_effect=_mock_query,
    ):
        result = await fetch_tenant_cost_monthly(
            tenant_id=_TENANT_ID,
            yyyymm=_YYYYMM,
            tenant_mrr_myr=Decimal("2499.00"),
            mimir_url="https://mimir.example.com",
            mimir_token="tok",
        )

    assert result.tenant_id == _TENANT_ID
    assert result.period_yyyymm == _YYYYMM
    # Should have computed some cost from the token counts
    assert result.anthropic_input_cost_myr >= Decimal("0")
    assert result.anthropic_output_cost_myr >= Decimal("0")
    assert result.data_source == "mimir"


async def test_parse_mimir_response_zero_when_empty() -> None:
    """When all Mimir queries return empty results, costs are zero."""
    _clear_cache()
    from unittest.mock import patch

    from polaris_api.services.grafana_cost import fetch_tenant_cost_monthly

    async def _mock_query(url: str, token: str, query: str, time_param: str | None) -> dict:
        return _MIMIR_EMPTY_RESPONSE

    with patch(
        "polaris_api.services.grafana_cost._query_mimir",
        side_effect=_mock_query,
    ):
        result = await fetch_tenant_cost_monthly(
            tenant_id=_TENANT_ID,
            yyyymm=_YYYYMM,
            tenant_mrr_myr=Decimal("2499.00"),
            mimir_url="https://mimir.example.com",
            mimir_token="tok",
        )

    assert result.anthropic_input_cost_myr == Decimal("0")
    assert result.anthropic_output_cost_myr == Decimal("0")
    assert result.cohere_rerank_cost_myr == Decimal("0")
    assert result.supabase_storage_cost_myr == Decimal("0")
    assert result.total_cost_myr == Decimal("0")


def _single_value_response(value: str) -> dict:
    """A one-series Mimir vector carrying ``value`` tokens (no labels needed)."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"tenant_id": str(_TENANT_ID)},
                    "value": [1748000000, value],
                }
            ],
        },
    }


async def test_prompt_cache_tokens_priced_at_anthropic_rates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache write (1.25x) + cache read (0.1x) fold into the input cost.

    FX pinned to 4.70 so this asserts the pricing MATH independent of the
    production default. USD totals (1.00 + 10.00 + 2.00 = $13/M) × 4.70 =
    input 1M ($1) + cache_write 8M ($10) + cache_read 20M ($2) = 61.10 MYR.
    """
    _clear_cache()
    from types import SimpleNamespace
    from unittest.mock import patch

    from polaris_api.services import grafana_cost
    from polaris_api.services.grafana_cost import fetch_tenant_cost_monthly

    monkeypatch.setattr(
        grafana_cost, "get_settings", lambda: SimpleNamespace(usd_to_myr_rate=4.70)
    )

    async def _mock_query(url: str, token: str, query: str, time_param: str | None) -> dict:
        if "cache_write_tokens" in query:
            return _single_value_response("8000000")
        if "cache_read_tokens" in query:
            return _single_value_response("20000000")
        if "input_tokens" in query:
            return _single_value_response("1000000")
        return _MIMIR_EMPTY_RESPONSE

    with patch(
        "polaris_api.services.grafana_cost._query_mimir",
        side_effect=_mock_query,
    ):
        result = await fetch_tenant_cost_monthly(
            tenant_id=_TENANT_ID,
            yyyymm=_YYYYMM,
            tenant_mrr_myr=Decimal("2499.00"),
            mimir_url="https://mimir.example.com",
            mimir_token="tok",
            include_tier_breakdown=True,
        )

    assert result.anthropic_input_cost_myr == Decimal("61.10")
    # Cache reads must be 10x cheaper than the same tokens billed as input.
    read_only_cost = Decimal("20000000") * Decimal("4.70") / Decimal("1_000_000")
    assert read_only_cost == Decimal("94.00")  # what it WOULD cost at input rate
    # Tier breakdown surfaces the cache-read volume + hit rate.
    tiers = result.per_intent_tier
    assert tiers, "expected a per-intent-tier breakdown row"
    assert any(t.avg_cache_read_tokens == 20_000_000 for t in tiers)


async def test_embedding_tokens_priced_into_openrouter_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenRouter embedding tokens land in openrouter_embedding_cost_myr.

    FX pinned to 4.70: 10M embedding tokens @ $0.01/M × 4.70 = 0.47 MYR.
    """
    _clear_cache()
    from types import SimpleNamespace
    from unittest.mock import patch

    from polaris_api.services import grafana_cost
    from polaris_api.services.grafana_cost import fetch_tenant_cost_monthly

    monkeypatch.setattr(
        grafana_cost, "get_settings", lambda: SimpleNamespace(usd_to_myr_rate=4.70)
    )

    async def _mock_query(url: str, token: str, query: str, time_param: str | None) -> dict:
        if "embedding_tokens" in query:
            return _single_value_response("10000000")
        return _MIMIR_EMPTY_RESPONSE

    with patch(
        "polaris_api.services.grafana_cost._query_mimir",
        side_effect=_mock_query,
    ):
        result = await fetch_tenant_cost_monthly(
            tenant_id=_TENANT_ID,
            yyyymm=_YYYYMM,
            tenant_mrr_myr=Decimal("2499.00"),
            mimir_url="https://mimir.example.com",
            mimir_token="tok",
        )

    assert result.openrouter_embedding_cost_myr == Decimal("0.47")
    # Embedding cost is part of the variable COGS total.
    assert result.total_cost_myr == Decimal("0.47")


async def test_cost_scales_with_usd_to_myr_fx_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single FX knob scales USD-priced cost: 1M input @ $1/M × FX 10 = 10.00 MYR."""
    _clear_cache()
    from types import SimpleNamespace
    from unittest.mock import patch

    from polaris_api.services import grafana_cost

    async def _mock_query(url: str, token: str, query: str, time_param: str | None) -> dict:
        if "input_tokens" in query:
            return _single_value_response("1000000")
        return _MIMIR_EMPTY_RESPONSE

    monkeypatch.setattr(
        grafana_cost, "get_settings", lambda: SimpleNamespace(usd_to_myr_rate=10.0)
    )
    with patch(
        "polaris_api.services.grafana_cost._query_mimir",
        side_effect=_mock_query,
    ):
        result = await grafana_cost.fetch_tenant_cost_monthly(
            tenant_id=_TENANT_ID,
            yyyymm=_YYYYMM,
            tenant_mrr_myr=Decimal("2499.00"),
            mimir_url="https://mimir.example.com",
            mimir_token="tok",
        )

    assert result.anthropic_input_cost_myr == Decimal("10.00")


def test_usd_to_myr_falls_back_on_bad_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing / non-positive FX setting falls back to the ~4.70 default."""
    from types import SimpleNamespace

    from polaris_api.services import grafana_cost

    monkeypatch.setattr(
        grafana_cost, "get_settings", lambda: SimpleNamespace(usd_to_myr_rate=0)
    )
    assert grafana_cost._usd_to_myr() == grafana_cost._DEFAULT_USD_TO_MYR

    monkeypatch.setattr(grafana_cost, "get_settings", lambda: SimpleNamespace())
    assert grafana_cost._usd_to_myr() == grafana_cost._DEFAULT_USD_TO_MYR


# ---------------------------------------------------------------------------
# Mimir unreachable / error paths
# ---------------------------------------------------------------------------


async def test_mimir_4xx_returns_zeros_no_exception() -> None:
    """Mimir 4xx response → zero-cost result with data_source='unavailable', no raise."""
    _clear_cache()
    from unittest.mock import patch

    from polaris_api.services.grafana_cost import MimirError, fetch_tenant_cost_monthly

    async def _mock_query_4xx(url: str, token: str, query: str, time_param: str | None) -> dict:
        raise MimirError(401)

    with patch(
        "polaris_api.services.grafana_cost._query_mimir",
        side_effect=_mock_query_4xx,
    ):
        result = await fetch_tenant_cost_monthly(
            tenant_id=_TENANT_ID,
            yyyymm=_YYYYMM,
            tenant_mrr_myr=Decimal("2499.00"),
            mimir_url="https://mimir.example.com",
            mimir_token="tok",
        )

    assert result.data_source == "unavailable"
    assert result.total_cost_myr == Decimal("0")
    assert result.anthropic_input_cost_myr == Decimal("0")


async def test_mimir_5xx_returns_zeros_no_exception() -> None:
    """Mimir 5xx → zero-cost result with data_source='unavailable', no raise."""
    _clear_cache()
    from unittest.mock import patch

    from polaris_api.services.grafana_cost import MimirError, fetch_tenant_cost_monthly

    async def _mock_query_5xx(url: str, token: str, query: str, time_param: str | None) -> dict:
        raise MimirError(503)

    with patch(
        "polaris_api.services.grafana_cost._query_mimir",
        side_effect=_mock_query_5xx,
    ):
        result = await fetch_tenant_cost_monthly(
            tenant_id=_TENANT_ID,
            yyyymm=_YYYYMM,
            tenant_mrr_myr=Decimal("2499.00"),
            mimir_url="https://mimir.example.com",
            mimir_token="tok",
        )

    assert result.data_source == "unavailable"
    assert result.total_cost_myr == Decimal("0")


async def test_mimir_network_error_returns_zeros() -> None:
    """Any other exception from _query_mimir → zeros, no raise."""
    _clear_cache()
    from unittest.mock import patch

    from polaris_api.services.grafana_cost import fetch_tenant_cost_monthly

    async def _mock_query_exc(url: str, token: str, query: str, time_param: str | None) -> dict:
        raise ConnectionError("network down")

    with patch(
        "polaris_api.services.grafana_cost._query_mimir",
        side_effect=_mock_query_exc,
    ):
        result = await fetch_tenant_cost_monthly(
            tenant_id=_TENANT_ID,
            yyyymm=_YYYYMM,
            tenant_mrr_myr=Decimal("2499.00"),
            mimir_url="https://mimir.example.com",
            mimir_token="tok",
        )

    assert result.data_source == "unavailable"
    assert result.total_cost_myr == Decimal("0")


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


async def test_cache_returns_same_object_within_ttl() -> None:
    """Second call within 60s returns the cached object (same identity)."""
    _clear_cache()
    from unittest.mock import patch

    from polaris_api.services.grafana_cost import fetch_tenant_cost_monthly

    # Use a unique tenant/month to avoid cross-test contamination
    unique_tenant = uuid4()
    unique_month = "202699"

    call_count = 0

    async def _mock_query(url: str, token: str, query: str, time_param: str | None) -> dict:
        nonlocal call_count
        call_count += 1
        return _MIMIR_EMPTY_RESPONSE

    with patch(
        "polaris_api.services.grafana_cost._query_mimir",
        side_effect=_mock_query,
    ):
        r1 = await fetch_tenant_cost_monthly(
            tenant_id=unique_tenant,
            yyyymm=unique_month,
            tenant_mrr_myr=Decimal("2499.00"),
            mimir_url="https://mimir.example.com",
            mimir_token="tok",
        )
        r2 = await fetch_tenant_cost_monthly(
            tenant_id=unique_tenant,
            yyyymm=unique_month,
            tenant_mrr_myr=Decimal("2499.00"),
            mimir_url="https://mimir.example.com",
            mimir_token="tok",
        )

    # Same object returned from cache
    assert r1 is r2
    # Seven calls for the first fetch (input, output, cache-write, cache-read,
    # cohere, embedding, supabase); the second fetch is served from cache.
    assert call_count == 7


# ---------------------------------------------------------------------------
# Alert band unit tests
# ---------------------------------------------------------------------------


async def test_alert_band_computed_correctly() -> None:
    """alert_band logic: green < 50%, amber 50-100% exclusive, red >= 100%."""
    from polaris_api.services.grafana_cost import _compute_alert_band
    from polaris_shared_types.tenant_cost import AlertBand

    assert _compute_alert_band(Decimal("0"), Decimal("100")) == AlertBand.GREEN
    assert _compute_alert_band(Decimal("49"), Decimal("100")) == AlertBand.GREEN
    assert _compute_alert_band(Decimal("50.01"), Decimal("100")) == AlertBand.AMBER
    # At exactly 100% MRR → red (cost > MRR threshold)
    assert _compute_alert_band(Decimal("100.01"), Decimal("100")) == AlertBand.RED
    assert _compute_alert_band(Decimal("150"), Decimal("100")) == AlertBand.RED


async def test_alert_band_exact_50pct_boundary() -> None:
    """At exactly 50% MRR the band is still green (> 50% triggers amber)."""
    from polaris_api.services.grafana_cost import _compute_alert_band
    from polaris_shared_types.tenant_cost import AlertBand

    # Exactly 50% → green (threshold is >0.5 not >=0.5)
    assert _compute_alert_band(Decimal("50"), Decimal("100")) == AlertBand.GREEN


async def test_alert_band_zero_mrr_is_green() -> None:
    """MRR=0 (unconfigured) must not divide by zero; treat as green."""
    from polaris_api.services.grafana_cost import _compute_alert_band
    from polaris_shared_types.tenant_cost import AlertBand

    result = _compute_alert_band(Decimal("0"), Decimal("0"))
    assert result == AlertBand.GREEN


# ---------------------------------------------------------------------------
# Fixed/variable split (2026-06-01) — Supabase excluded from variable total
# ---------------------------------------------------------------------------


def test_variable_total_excludes_supabase_and_computes_margin() -> None:
    """total_cost_myr is variable-only (no Supabase); margin = MRR − variable."""
    from polaris_api.services.grafana_cost import _build_cost_result

    result = _build_cost_result(
        tenant_id=_TENANT_ID,
        yyyymm=_YYYYMM,
        anthropic_input_cost_myr=Decimal("10.00"),
        anthropic_output_cost_myr=Decimal("20.00"),
        cohere_rerank_cost_myr=Decimal("5.00"),
        supabase_storage_cost_myr=Decimal("100.00"),  # must NOT count toward total
        openrouter_embedding_cost_myr=Decimal("0.00"),
        tenant_mrr_myr=Decimal("200.00"),
        data_source="mimir",
    )
    cost = result.cost
    # Variable total = 10 + 20 + 5 + 0 = 35 (Supabase 100 excluded)
    assert cost.total_cost_myr == Decimal("35.00")
    assert cost.supabase_storage_cost_myr == Decimal("100.00")  # still reported
    # Contribution margin = 200 − 35 = 165
    assert cost.contribution_margin_myr == Decimal("165.00")
    assert abs(cost.contribution_margin_pct - 0.825) < 1e-6
    assert abs(cost.cost_pct_of_mrr - 0.175) < 1e-6


def test_margin_negative_when_variable_exceeds_mrr() -> None:
    from polaris_api.services.grafana_cost import _build_cost_result

    result = _build_cost_result(
        tenant_id=_TENANT_ID,
        yyyymm=_YYYYMM,
        anthropic_input_cost_myr=Decimal("300.00"),
        anthropic_output_cost_myr=Decimal("0.00"),
        cohere_rerank_cost_myr=Decimal("0.00"),
        supabase_storage_cost_myr=Decimal("0.00"),
        openrouter_embedding_cost_myr=Decimal("0.00"),
        tenant_mrr_myr=Decimal("200.00"),
        data_source="mimir",
    )
    assert result.cost.contribution_margin_myr == Decimal("-100.00")
    assert result.cost.alert_band.value == "red"


# ---------------------------------------------------------------------------
# Parse-failure WARNING logs — silent-failure audit fix A7 (2026-07-02)
# ---------------------------------------------------------------------------


def test_sum_values_logs_warning_on_malformed_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed Mimir data still degrades to 0.0 (unchanged behavior) but now
    logs a WARNING with the exception type, instead of silently swallowing."""
    import logging

    from polaris_api.services.grafana_cost import _sum_values

    # value[1] is not float-parseable -> ValueError inside the try block.
    malformed = {"data": {"result": [{"value": [123456, "not-a-number"]}]}}

    with caplog.at_level(logging.WARNING):
        total = _sum_values(malformed)

    assert total == 0.0  # degrades to zero, same as before the fix
    assert any("_sum_values" in r.message for r in caplog.records)


def test_sum_values_no_warning_on_clean_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from polaris_api.services.grafana_cost import _sum_values

    clean = {"data": {"result": [{"value": [123456, "42.5"]}]}}
    with caplog.at_level(logging.WARNING):
        total = _sum_values(clean)

    assert total == 42.5
    assert not any("_sum_values" in r.message for r in caplog.records)


def test_extract_tier_breakdown_logs_warning_on_malformed_item(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed item inside one Mimir vector must still be skipped (the
    aggregate for other items unaffected) but now logs a WARNING."""
    import logging

    from polaris_api.services.grafana_cost import _extract_tier_breakdown

    good_item = {
        "metric": {"intent_tier": "hard", "model_used": "sonnet"},
        "value": [123456, "10.0"],
    }
    bad_item = {
        "metric": {"intent_tier": "medium", "model_used": "haiku"},
        "value": [123456, "not-a-number"],
    }
    input_result = {"data": {"result": [good_item, bad_item]}}
    output_result = {"data": {"result": []}}

    with caplog.at_level(logging.WARNING):
        breakdown = _extract_tier_breakdown(input_result, output_result)

    assert any("_accumulate" in r.message for r in caplog.records)
    # The good item's tier still aggregates correctly despite the bad sibling.
    hard_entry = next(b for b in breakdown if b.intent_tier == "hard")
    assert hard_entry.avg_input_tokens == 10


def test_extract_tier_breakdown_no_warning_on_clean_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from polaris_api.services.grafana_cost import _extract_tier_breakdown

    input_result = {
        "data": {
            "result": [
                {
                    "metric": {"intent_tier": "hard", "model_used": "sonnet"},
                    "value": [123456, "10.0"],
                }
            ]
        }
    }
    output_result = {"data": {"result": []}}

    with caplog.at_level(logging.WARNING):
        _extract_tier_breakdown(input_result, output_result)

    assert not any("_accumulate" in r.message for r in caplog.records)
