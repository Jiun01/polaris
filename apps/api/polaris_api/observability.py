"""Polaris API observability bootstrap — Phase 10 PR-2.

Encapsulates Sentry init + OTel auto-instrumentation behind a single
``configure_observability()`` call so ``main.py`` lifespan stays clean.

Both subsystems degrade gracefully:
  - Sentry: if ``sentry_dsn`` is empty, Sentry is disabled (OK for local).
  - OTel:   if ``otel_endpoint`` is empty, OTel is disabled.
            If the OTel extra packages are not installed, each instrumentor
            logs a DEBUG message and skips — so ``uv sync`` is not required
            in local dev unless you actually want traces.

``configure_observability()`` is idempotent (guarded by ``_configured``).

ADR refs:
  ADR-016 — Grafana Cloud OTLP only; no self-hosted Grafana at GA.
  ADR-035 — Sentry per-tenant 5×10min alert rule (configured in Sentry
             project settings, not in code); phonenumbers lib E.164;
             send_default_pii=False.

Environment variables (consumed via Settings, passed as kwargs):
  SENTRY_DSN        — Sentry ingest URL; empty = disabled
  OTEL_ENDPOINT     — OTLP HTTP gateway URL
                      e.g. https://otlp-gateway-prod-<region>.grafana.net/otlp
  OTEL_SERVICE_NAME — service.name resource attribute (default: "polaris-api")
  OTEL_HEADERS      — comma-separated "key=value" auth pairs
                      e.g. "Authorization=Basic <base64(instanceId:apiKey)>"
  VERSION           — commit SHA (injected at container build time)
  ENVIRONMENT       — local | staging | prod
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_configured = False


def configure_observability(
    *,
    sentry_dsn: str,
    otel_endpoint: str,
    otel_service_name: str,
    otel_headers: str,
    environment: str,
    release: str,
) -> None:
    """Initialise Sentry + OTel once.  Safe to call multiple times (idempotent)."""
    global _configured
    if _configured:
        return
    _configure_sentry(dsn=sentry_dsn, environment=environment, release=release)
    _configure_otel(
        endpoint=otel_endpoint,
        service_name=otel_service_name,
        headers=otel_headers,
        environment=environment,
        release=release,
    )
    _configured = True


# ── Sentry ────────────────────────────────────────────────────────────────────


def _configure_sentry(*, dsn: str, environment: str, release: str) -> None:
    if not dsn:
        logger.info("observability.sentry: DSN not set — disabled (OK for local)")
        return
    try:
        import sentry_sdk  # noqa: PLC0415
    except ImportError:
        logger.warning("observability.sentry: sentry-sdk not installed — skipped")
        return

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        # 10 % trace sample: Grafana Cloud OTLP carries spans; Sentry is for
        # errors + alerts only (ADR-035).  Raise in prod if error-rate
        # correlation needs full trace context.
        traces_sample_rate=0.1,
        # Profiling disabled at GA — revisit once Python profiling ROI is clear.
        profiles_sample_rate=0.0,
        # Never leak user PII (emails, IPs) to Sentry servers.
        send_default_pii=False,
        # Per-tenant 5×10min alert rule is configured in the Sentry project
        # settings UI (ADR-035) — kept out of code so ops can tune without
        # a deploy.
    )
    logger.info(
        "observability.sentry: initialised (env=%s release=%s)",
        environment,
        release,
    )


# ── OpenTelemetry ─────────────────────────────────────────────────────────────


def _configure_otel(
    *,
    endpoint: str,
    service_name: str,
    headers: str,
    environment: str,
    release: str,
) -> None:
    if not endpoint:
        logger.info(
            "observability.otel: OTEL_ENDPOINT not set — disabled (OK for local)"
        )
        return

    # Core SDK — lazy import so a bare local install (no OTel extras) still boots.
    try:
        from opentelemetry import trace  # noqa: PLC0415
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415
    except ImportError:
        logger.warning(
            "observability.otel: opentelemetry-sdk not installed — OTel skipped. "
            "Run `uv sync` to install the extras added in Phase 10 PR-2."
        )
        return

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": release,
            "deployment.environment": environment,
        }
    )

    # Parse "Authorization=Basic xxx,X-Scope-OrgID=1" → {"Authorization": "Basic xxx", …}
    headers_dict: dict[str, str] = {}
    if headers:
        for pair in headers.split(","):
            if "=" in pair:
                k, _, v = pair.partition("=")
                headers_dict[k.strip()] = v.strip()

    exporter = OTLPSpanExporter(
        endpoint=endpoint.rstrip("/") + "/v1/traces",
        headers=headers_dict,
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    _instrument_fastapi()
    _instrument_asyncpg()
    _instrument_httpx()

    # Metric pipeline — feeds the per-tenant cost counters defined in
    # ``metrics.py`` to the admin cost dashboard (separate from spans above).
    _configure_otel_metrics(endpoint=endpoint, headers=headers_dict, resource=resource)

    logger.info(
        "observability.otel: initialised (service=%s endpoint=%s env=%s)",
        service_name,
        endpoint,
        environment,
    )


def _configure_otel_metrics(
    *,
    endpoint: str,
    headers: dict[str, str],
    resource: object,
) -> None:
    """Install the OTLP metric pipeline feeding the admin cost dashboard.

    The dashboard (``routers/admin_cost.py`` → ``services/grafana_cost.py``)
    queries these Mimir series for per-tenant COGS:

      * ``polaris_anthropic_input_tokens_total{tenant_id,intent_tier,model_used}``
      * ``polaris_anthropic_output_tokens_total{...}``
      * ``polaris_cohere_rerank_calls_total{tenant_id}``

    The counters themselves live in ``metrics.py`` (named without the
    ``_total`` suffix — Grafana Cloud appends it on ingest; see the note
    there). This wires a ``PeriodicExportingMetricReader`` → OTLP HTTP
    exporter at ``<endpoint>/v1/metrics``, exporting once a minute (cost
    metrics are low-frequency; the dashboard caches for 60s anyway).

    Degrades the same way as the trace path: if the OTLP metric exporter
    package isn't installed it logs and skips, leaving the no-op global
    meter in place.
    """
    try:
        from opentelemetry import metrics  # noqa: PLC0415
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # noqa: PLC0415
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider  # noqa: PLC0415
        from opentelemetry.sdk.metrics.export import (  # noqa: PLC0415
            PeriodicExportingMetricReader,
        )
    except ImportError:
        logger.warning(
            "observability.otel: opentelemetry metrics SDK not installed — "
            "cost counters skipped. Run `uv sync`."
        )
        return

    exporter = OTLPMetricExporter(
        endpoint=endpoint.rstrip("/") + "/v1/metrics",
        headers=headers,
    )
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=60_000)
    provider = MeterProvider(resource=resource, metric_readers=[reader])  # type: ignore[arg-type]
    metrics.set_meter_provider(provider)
    logger.debug("observability.otel: metric pipeline initialised (interval=60s)")


def _instrument_fastapi() -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415

        FastAPIInstrumentor().instrument()
        logger.debug("observability.otel: FastAPI instrumented")
    except ImportError:
        logger.debug(
            "observability.otel: opentelemetry-instrumentation-fastapi not installed"
        )


def _instrument_asyncpg() -> None:
    try:
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor  # noqa: PLC0415

        AsyncPGInstrumentor().instrument()  # type: ignore[no-untyped-call]
        logger.debug("observability.otel: asyncpg instrumented")
    except ImportError:
        logger.debug(
            "observability.otel: opentelemetry-instrumentation-asyncpg not installed"
        )


def _instrument_httpx() -> None:
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: PLC0415

        HTTPXClientInstrumentor().instrument()
        logger.debug("observability.otel: httpx instrumented")
    except ImportError:
        logger.debug(
            "observability.otel: opentelemetry-instrumentation-httpx not installed"
        )
