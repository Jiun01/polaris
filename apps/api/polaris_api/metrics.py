"""Polaris cost metrics — OTel counters feeding the admin cost dashboard.

The per-tenant cost dashboard (``routers/admin_cost.py`` →
``services/grafana_cost.py``) reads four Prometheus/Mimir series via PromQL:

  * ``polaris_anthropic_input_tokens_total{tenant_id, intent_tier, model_used}``
  * ``polaris_anthropic_output_tokens_total{tenant_id, intent_tier, model_used}``
  * ``polaris_anthropic_cache_write_tokens_total{tenant_id, intent_tier, model_used}``
  * ``polaris_anthropic_cache_read_tokens_total{tenant_id, intent_tier, model_used}``
  * ``polaris_cohere_rerank_calls_total{tenant_id}``
  * ``polaris_openrouter_embedding_tokens_total{tenant_id}`` — query/document
    embedding tokens billed by OpenRouter (qwen3-embedding). Tiny per message
    but tracked so the dashboard shows a measured figure, not a false zero.
  * ``polaris_supabase_cost_myr_total{tenant_id}`` — an operator/infra gauge,
    NOT emitted from the request path; left for the ops team to push.

Anthropic prompt caching splits the input bill three ways and we track all
three so the dashboard prices them correctly: ``input_tokens`` is the
*uncached* prompt (1× rate), cache WRITES bill ~1.25×, cache READS bill
~0.1×. Anthropic reports these as three disjoint usage fields, so the cache
counters are NOT a subset of ``input_tokens`` — they are additive.

This module defines the three request-path counters and the helpers the
LLM / RAG call-sites use to record them. The OTLP metric pipeline that
exports them to Grafana Cloud is configured in ``observability.py``.

THE ``_total`` SUFFIX (read before renaming):
    The instruments here are named WITHOUT the ``_total`` suffix
    (``polaris_anthropic_input_tokens``, not ``..._total``). Grafana
    Cloud's OTLP→Prometheus normalization appends ``_total`` to every
    monotonic counter on ingest, which is what produces the ``..._total``
    series the dashboard's PromQL queries. If your Mimir ingest has
    suffix normalization DISABLED, the series will land without ``_total``
    and the dashboard will read empty — in that case add ``_total`` to the
    instrument names below (and keep them in sync with the query builders
    in ``grafana_cost.py``).

No-op safety:
    When ``OTEL_ENDPOINT`` is unset (local dev) no MeterProvider is
    installed, so the OTel global meter is the API's no-op proxy and every
    ``record_*`` call is a cheap no-op. Call-sites therefore need no
    guards. Instruments are created at import time; OTel's proxy-meter
    machinery rebinds them to the real provider once
    ``set_meter_provider`` runs in the app lifespan.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)


try:
    from opentelemetry import metrics as _otel_metrics

    _meter = _otel_metrics.get_meter("polaris.cost")
    _anthropic_input_tokens = _meter.create_counter(
        name="polaris_anthropic_input_tokens",
        unit="",
        description="Anthropic input (prompt) tokens consumed, by tenant/tier/model.",
    )
    _anthropic_output_tokens = _meter.create_counter(
        name="polaris_anthropic_output_tokens",
        unit="",
        description="Anthropic output (completion) tokens generated, by tenant/tier/model.",
    )
    _anthropic_cache_write_tokens = _meter.create_counter(
        name="polaris_anthropic_cache_write_tokens",
        unit="",
        description=(
            "Anthropic prompt-cache WRITE tokens (cache_creation, billed ~1.25x "
            "input), by tenant/tier/model."
        ),
    )
    _anthropic_cache_read_tokens = _meter.create_counter(
        name="polaris_anthropic_cache_read_tokens",
        unit="",
        description=(
            "Anthropic prompt-cache READ tokens (cache_read, billed ~0.1x input), "
            "by tenant/tier/model."
        ),
    )
    _cohere_rerank_calls = _meter.create_counter(
        name="polaris_cohere_rerank_calls",
        unit="",
        description="Cohere rerank API calls, by tenant.",
    )
    _cohere_rerank_degrades = _meter.create_counter(
        name="polaris_cohere_rerank_degrades",
        unit="",
        description=(
            "Cohere rerank calls that degraded to un-reranked hybrid results "
            "due to a network/API failure (NOT the config-disabled case), by "
            "tenant/reason. A non-zero rate means Ultra/Enterprise tenants are "
            "silently losing rerank quality — investigate the Cohere API "
            "status page or COHERE_API_KEY rate limits."
        ),
    )
    _openrouter_embedding_tokens = _meter.create_counter(
        name="polaris_openrouter_embedding_tokens",
        unit="",
        description="OpenRouter embedding tokens consumed (qwen3-embedding), by tenant.",
    )
    _outbound_dispatch_failures = _meter.create_counter(
        name="polaris_outbound_dispatch_failures",
        unit="",
        description=(
            "Outbound reply sends that did NOT reach the customer, by "
            "tenant/channel/reason/kind. ``kind`` is 'permanent' (4xx, missing "
            "credentials, misconfig — operator action needed) or 'transient' "
            "(5xx, network — redelivery/retry covers it). A non-zero rate here "
            "means replies are being silently lost; alert on it."
        ),
    )
    _outbound_image_mint_drops = _meter.create_counter(
        name="polaris_outbound_image_mint_drops",
        unit="",
        description=(
            "Product images dropped at send time because their signed read URL "
            "could not be minted, by tenant/scope. ``scope='partial'`` — some "
            "images sent; ``scope='total'`` — an image-centric reply lost ALL "
            "its images (text still sent)."
        ),
    )
    _channel_reconcile_fixes = _meter.create_counter(
        name="polaris_channel_reconcile_fixes",
        unit="",
        description=(
            "Channel reconciliation issues found by the periodic self-heal job, "
            "by tenant/kind ('agent_bot_attach_failed' or 'token_missing'). A "
            "sustained non-zero rate for the same tenant means the hourly job "
            "isn't fixing it (Chatwoot outage, or a token that needs manual "
            "re-provisioning) — alert on repeat offenders."
        ),
    )
    _ENABLED = True
except Exception:  # pragma: no cover - defensive: OTel API absent in a stripped build
    logger.debug("metrics: opentelemetry API unavailable — cost counters disabled")
    _ENABLED = False


def record_anthropic_usage(
    *,
    tenant_id: UUID,
    intent_tier: str,
    model_used: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> None:
    """Record one LLM reply's token usage on the per-tenant cost counters.

    ``intent_tier`` and ``model_used`` become metric labels so the dashboard
    can break cost down per tier/model. ``cache_write_tokens`` /
    ``cache_read_tokens`` are Anthropic's prompt-cache creation/read counts —
    additive to ``input_tokens`` (Anthropic reports the three disjointly) and
    priced differently downstream. Zero-valued counts are skipped (no series
    churn for stubbed/empty/uncached replies). Never raises — a metrics
    failure must not break a customer reply.
    """
    if not _ENABLED:
        return
    try:
        attrs = {
            "tenant_id": str(tenant_id),
            "intent_tier": intent_tier,
            "model_used": model_used,
        }
        if input_tokens:
            _anthropic_input_tokens.add(input_tokens, attrs)
        if output_tokens:
            _anthropic_output_tokens.add(output_tokens, attrs)
        if cache_write_tokens:
            _anthropic_cache_write_tokens.add(cache_write_tokens, attrs)
        if cache_read_tokens:
            _anthropic_cache_read_tokens.add(cache_read_tokens, attrs)
    except Exception:  # pragma: no cover - telemetry must never break the reply path
        logger.debug("metrics.record_anthropic_usage failed", exc_info=True)


def record_cohere_rerank(*, tenant_id: UUID, count: int = 1) -> None:
    """Record ``count`` Cohere rerank API calls for a tenant. Never raises."""
    if not _ENABLED or not count:
        return
    try:
        _cohere_rerank_calls.add(count, {"tenant_id": str(tenant_id)})
    except Exception:  # pragma: no cover - telemetry must never break retrieval
        logger.debug("metrics.record_cohere_rerank failed", exc_info=True)


def record_cohere_rerank_degrade(*, tenant_id: UUID, reason: str) -> None:
    """Record one Cohere rerank degrade-to-unranked event. Never raises."""
    if not _ENABLED:
        return
    try:
        _cohere_rerank_degrades.add(1, {"tenant_id": str(tenant_id), "reason": reason})
    except Exception:  # pragma: no cover - telemetry must never break retrieval
        logger.debug("metrics.record_cohere_rerank_degrade failed", exc_info=True)


def record_openrouter_embedding(*, tenant_id: UUID, tokens: int) -> None:
    """Record OpenRouter embedding tokens for a tenant. Never raises."""
    if not _ENABLED or not tokens:
        return
    try:
        _openrouter_embedding_tokens.add(tokens, {"tenant_id": str(tenant_id)})
    except Exception:  # pragma: no cover - telemetry must never break embedding
        logger.debug("metrics.record_openrouter_embedding failed", exc_info=True)


def record_channel_reconcile_fix(*, tenant_id: UUID, kind: str) -> None:
    """Record one channel-reconciliation issue found. Never raises."""
    if not _ENABLED:
        return
    try:
        _channel_reconcile_fixes.add(1, {"tenant_id": str(tenant_id), "kind": kind})
    except Exception:  # pragma: no cover - telemetry must never break reconciliation
        logger.debug("metrics.record_channel_reconcile_fix failed", exc_info=True)


def record_dispatch_failure(
    *,
    tenant_id: UUID,
    channel: str,
    reason: str,
    kind: Literal["permanent", "transient"],
) -> None:
    """Record one outbound reply that failed to reach the customer.

    ``kind`` is 'permanent' (4xx / missing credentials / misconfig — needs
    operator action) or 'transient' (5xx / network — redelivery covers it).
    ``reason`` is a low-cardinality tag (e.g. 'missing_credentials',
    'http_400', 'network'). Emitted from ``pipeline._dispatch_reply`` so the
    Grafana stack tracks silent reply loss. Never raises.
    """
    if not _ENABLED:
        return
    try:
        _outbound_dispatch_failures.add(
            1,
            {
                "tenant_id": str(tenant_id),
                "channel": channel,
                "reason": reason,
                "kind": kind,
            },
        )
    except Exception:  # pragma: no cover - telemetry must never break dispatch
        logger.debug("metrics.record_dispatch_failure failed", exc_info=True)


def record_image_mint_drops(
    *,
    tenant_id: UUID,
    dropped: int,
    scope: Literal["partial", "total"],
) -> None:
    """Record ``dropped`` product images lost at send time (URL mint failure).

    ``scope`` is 'partial' (some images still sent) or 'total' (every image on
    an image-centric reply was lost; the text still went out). Emitted from
    ``pipeline._mint_product_image_urls``. Never raises.
    """
    if not _ENABLED or not dropped:
        return
    try:
        _outbound_image_mint_drops.add(
            dropped, {"tenant_id": str(tenant_id), "scope": scope}
        )
    except Exception:  # pragma: no cover - telemetry must never break the reply
        logger.debug("metrics.record_image_mint_drops failed", exc_info=True)
