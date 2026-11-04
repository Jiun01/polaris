"""Platform-health snapshot — Pydantic SSOT (Phase 9 PR-1; PR-6 read path).

Aggregate of the signals shown on the admin System Health page. Auto-refreshed
every 5 minutes per Q-PHASE9-11 (tab-visibility-gated; manual "Refresh now"
button on the page). FastAPI caches the underlying checks for 60 s so the
5-min admin refresh doesn't hammer Supabase or Redis.

Signals (post-enrichment 2026-06-01):
  - Supabase / Redis connectivity
  - Broadcast queue depth
  - Messaging volume (inbound/outbound + bot replies)
  - LLM token burn (+ estimated MYR)
  - Quota saturation (tenants near/over bot-reply cap)
  - Recent failures + APM deep-links (Grafana / Sentry)
  - Meta Graph API rate-limit headroom

The legacy per-tenant Ingestion tile was dropped (ingestion was soft-retired
to a separate project per ADR-057).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")


class SupabaseStatus(BaseModel):
    """Supabase connectivity probe.

    A simple ``SELECT 1`` against the polaris_service pool plus the
    timing. ``healthy=False`` + error string when the probe fails;
    db_latency_ms NULL in that case.
    """

    model_config = _FORBID

    healthy: bool
    last_checked_at: str = Field(..., description="RFC3339 UTC.")
    db_latency_ms: int | None = Field(
        None,
        ge=0,
        description="60-second rolling average; NULL when last probe failed.",
    )
    error: str | None = Field(None, max_length=500)


class RedisStatus(BaseModel):
    """Redis connectivity probe.

    ``INFO`` aggregation; used_memory_mb shown alongside healthy/error.
    """

    model_config = _FORBID

    healthy: bool
    last_checked_at: str
    used_memory_mb: int | None = Field(
        None,
        ge=0,
        description="Reported by INFO; NULL when last probe failed.",
    )
    error: str | None = Field(None, max_length=500)


class BroadcastQueueDepth(BaseModel):
    """Broadcast worker queue snapshot from Phase 6.

    Counters drawn from the broadcasts table (status enum
    draft|scheduled|running|paused|completed|cancelled|errored) plus the
    broadcast_recipients table (failed_last_hour).
    """

    model_config = _FORBID

    pending: int = Field(..., ge=0, description="Broadcasts scheduled, not yet running.")
    in_flight: int = Field(..., ge=0, description="Broadcasts currently running.")
    failed_last_hour: int = Field(..., ge=0)


class MessagingVolume(BaseModel):
    """Conversation throughput from the ``messages`` table.

    Direction inferred from ``role`` (user = inbound, assistant = outbound).
    bot_replies_mtd comes from ``tenant_usage`` (metered bot replies this
    billing month, summed across tenants).
    """

    model_config = _FORBID

    inbound_24h: int = Field(..., ge=0)
    outbound_24h: int = Field(..., ge=0)
    bot_replies_mtd: int = Field(..., ge=0, description="Metered bot replies, month-to-date.")
    last_message_at: str | None = Field(None, description="RFC3339 UTC of newest message.")


class LlmTokenBurn(BaseModel):
    """LLM token throughput from the ``messages`` table.

    Authoritative MYR spend lives in Grafana Cloud (the Cost dashboard);
    ``est_cost_myr_mtd`` here is a local best-effort estimate from token
    counts and is NULL when no pricing map is available.
    """

    model_config = _FORBID

    input_tokens_24h: int = Field(..., ge=0)
    output_tokens_24h: int = Field(..., ge=0)
    input_tokens_mtd: int = Field(..., ge=0)
    output_tokens_mtd: int = Field(..., ge=0)
    est_cost_myr_mtd: float | None = Field(
        None, ge=0, description="Local estimate; Cost dashboard (Mimir) is source of record."
    )


class QuotaSaturationTenant(BaseModel):
    """A single tenant's bot-reply quota saturation this period."""

    model_config = _FORBID

    tenant_id: str
    tenant_name: str | None = None
    plan_tier: str | None = None
    bot_replies: int = Field(..., ge=0, description="Consumed this period.")
    cap: int | None = Field(None, ge=0, description="Plan allowance; NULL = uncapped/unknown.")
    usage_pct: float | None = Field(None, ge=0, description="consumed / cap * 100.")


class QuotaSaturation(BaseModel):
    """Bot-reply quota saturation across tenants (tenant_usage vs tenant_quotas)."""

    model_config = _FORBID

    near_cap_count: int = Field(..., ge=0, description="Tenants at >= 80% and < 100%.")
    over_cap_count: int = Field(..., ge=0, description="Tenants at >= 100%.")
    top: list[QuotaSaturationTenant] = Field(
        default_factory=list, description="Highest-saturation tenants (descending)."
    )


class RecentFailures(BaseModel):
    """Recent failure counters + deep-links to the full APM tooling."""

    model_config = _FORBID

    errored_broadcasts_last_hour: int = Field(..., ge=0)
    failed_recipients_last_hour: int = Field(..., ge=0)
    grafana_url: str | None = Field(None, description="Grafana dashboard URL when configured.")
    sentry_url: str | None = Field(None, description="Sentry project URL when configured.")


class MetaRateLimitHeadroom(BaseModel):
    """Meta Graph API rate-limit headroom per surface.

    Polaris captures the ``X-Business-Use-Case-Usage`` header from
    every Meta call and averages it over the last 24 h. calls_remaining_pct
    is 100 - usage (so 100% = full headroom, 0% = throttled).
    """

    model_config = _FORBID

    surface: str = Field(
        ...,
        description="whatsapp | instagram | messenger.",
    )
    calls_remaining_pct: float = Field(..., ge=0, le=100)
    last_observed_at: str = Field(..., description="RFC3339 UTC.")


class EmailDraftQueue(BaseModel):
    """Email assist draft queue snapshot from Phase 14.

    Pending drafts are Chatwoot private notes awaiting agent action.
    Accepted/edited/ignored counts are resolved_at outcomes over the last 24 h.
    """

    model_config = _FORBID

    pending: int = Field(..., ge=0, description="Draft posts awaiting agent action.")
    accepted_24h: int = Field(..., ge=0, description="Drafts accepted by agent in last 24 h.")
    edited_24h: int = Field(..., ge=0, description="Drafts edited before send in last 24 h.")
    ignored_24h: int = Field(..., ge=0, description="Drafts ignored/dismissed in last 24 h.")
    latest_draft_at: str | None = Field(None, description="RFC3339 UTC of most recent draft post.")


class KnowledgeGapCaptures(BaseModel):
    """Knowledge gap miss captures from Phase 15 (Ultra-tier only).

    Counts are drawn from knowledge_gap_misses over the last 24 h, grouped
    by signal_type (no_retrieval | low_retrieval | handoff).
    """

    model_config = _FORBID

    no_retrieval_24h: int = Field(..., ge=0, description="Misses with signal_type=no_retrieval.")
    low_retrieval_24h: int = Field(..., ge=0, description="Misses with signal_type=low_retrieval.")
    handoff_24h: int = Field(..., ge=0, description="Misses with signal_type=handoff.")
    total_24h: int = Field(..., ge=0, description="Sum of all signal types over last 24 h.")
    latest_at: str | None = Field(None, description="RFC3339 UTC of most recent gap capture.")


class ActiveTenants(BaseModel):
    """Active tenant counts from the messages and tenants tables.

    active_24h is distinct tenants that sent or received a message in the
    last 24 h; total_provisioned is non-deleted tenants in the tenants table.
    """

    model_config = _FORBID

    active_24h: int = Field(..., ge=0, description="Tenants with message activity in last 24 h.")
    total_provisioned: int = Field(..., ge=0, description="Non-deleted tenants total.")


class ServiceConnectivity(BaseModel):
    """HTTP reachability probe for an external service (Chatwoot or EA fork).

    healthy=True when the service returns any HTTP response (even 4xx/401);
    healthy=False on connection error or timeout. latency_ms is NULL when
    the probe failed. Reused for both the chatwoot and ea tiles.
    """

    model_config = _FORBID

    healthy: bool
    last_checked_at: str = Field(..., description="RFC3339 UTC of this probe.")
    latency_ms: int | None = Field(None, ge=0, description="Round-trip ms; NULL on failure.")
    error: str | None = Field(None, max_length=500)


class WebhookSurfaceHealth(BaseModel):
    """Webhook surface counters drawn from webhook_events.

    Reused for both telegram and meta webhook surfaces.
    events_1h, hmac_fail_1h, and tenant_not_found_1h are windowed to the
    last 1 h to give a fast-moving operational signal.
    """

    model_config = _FORBID

    last_received_at: str | None = Field(
        None, description="RFC3339 UTC of most recent webhook event."
    )
    events_1h: int = Field(..., ge=0, description="Total webhook events in last 1 h.")
    hmac_fail_1h: int = Field(..., ge=0, description="HMAC validation failures in last 1 h.")
    tenant_not_found_1h: int = Field(
        ..., ge=0, description="Events with no matching tenant in last 1 h."
    )


class ApmErrors(BaseModel):
    """Sentry error count for the last 24 h (optional — requires sentry_auth_token).

    configured=False when the token is not set; error_count_24h is NULL in
    that case.  On a Sentry API error: configured=True, error_count_24h=NULL,
    error contains the exception message.
    """

    model_config = _FORBID

    configured: bool = Field(..., description="True when sentry_auth_token is set.")
    error_count_24h: int | None = Field(
        None, ge=0, description="Sum of received errors over last 24 h from Sentry stats API."
    )
    sentry_url: str | None = Field(None, description="Sentry project browser URL when configured.")
    error: str | None = Field(None, max_length=500)


class WebhookEventCount(BaseModel):
    """One (key, count) bucket in the webhook-error breakdown.

    ``key`` is a ``webhook_events.status`` value (e.g. hmac_fail) or an
    ``error_class`` (e.g. ChannelNotEntitled:whatsapp), depending on which list
    it appears in.
    """

    model_config = _FORBID

    key: str = Field(..., max_length=200, description="A webhook_events.status value or error_class.")
    count: int = Field(..., ge=0)


class WebhookEventError(BaseModel):
    """One recent non-success ``webhook_events`` row for the at-a-glance list."""

    model_config = _FORBID

    received_at: str = Field(..., description="RFC3339 UTC.")
    source: str = Field(..., max_length=50, description="webhook_events.source (meta | chatwoot | …).")
    status: str = Field(..., max_length=50, description="webhook_events.status.")
    error_class: str | None = Field(None, max_length=200)


class WebhookEventBreakdown(BaseModel):
    """Webhook ingest errors over the last 24 h, grouped from ``webhook_events``.

    Surfaces the operational ingest problems the volume/freshness webhook tiles
    don't break out: HMAC failures, tenant-not-found, ChannelNotEntitled,
    suspended-tenant drops. An "error" is any status NOT in the benign set
    (received/processed/replay_ignored/human_handling/demo_routed/
    non_message_event/chatwoot_deferred). ``by_status`` and ``by_error_class`` are
    two additive views of the same total (both sum to ``total_errors_24h``);
    ``by_error_class`` aggregates an error_class ACROSS statuses and groups NULL
    under '(none)'. Lists are capped server-side (top-N status/error_class, most
    recent N rows). ``error`` is set ONLY when the probe itself failed (DB
    outage) — the tile must show a distinct state, since all-zero counts would
    otherwise read as a healthy "no errors".
    """

    model_config = _FORBID

    total_errors_24h: int = Field(..., ge=0)
    by_status: list[WebhookEventCount]
    by_error_class: list[WebhookEventCount]
    recent: list[WebhookEventError]
    grafana_url: str | None = Field(None, description="Grafana logs/explore URL when configured.")
    error: str | None = Field(None, max_length=500, description="Set when the probe failed.")


class HealthSnapshot(BaseModel):
    """Aggregate read for ``GET /api/admin/health``.

    Signals are loaded in parallel server-side; an outage on any one
    renders that tile only (no fail-the-whole-page) per the
    silent-failure-hunter gate in PR-6.
    """

    model_config = _FORBID

    supabase: SupabaseStatus
    redis: RedisStatus
    broadcasts: BroadcastQueueDepth
    messaging: MessagingVolume
    llm: LlmTokenBurn
    quota: QuotaSaturation
    failures: RecentFailures
    meta_rate_limits: list[MetaRateLimitHeadroom]
    email_drafts: EmailDraftQueue
    knowledge_gaps: KnowledgeGapCaptures
    active_tenants: ActiveTenants
    chatwoot: ServiceConnectivity
    ea: ServiceConnectivity
    telegram_webhooks: WebhookSurfaceHealth
    meta_webhooks: WebhookSurfaceHealth
    webhook_event_errors: WebhookEventBreakdown
    apm: ApmErrors
    refreshed_at: str = Field(..., description="RFC3339 UTC of this snapshot.")


__all__ = [
    "ActiveTenants",
    "ApmErrors",
    "BroadcastQueueDepth",
    "EmailDraftQueue",
    "HealthSnapshot",
    "KnowledgeGapCaptures",
    "LlmTokenBurn",
    "MessagingVolume",
    "MetaRateLimitHeadroom",
    "QuotaSaturation",
    "QuotaSaturationTenant",
    "RecentFailures",
    "RedisStatus",
    "ServiceConnectivity",
    "SupabaseStatus",
    "WebhookEventBreakdown",
    "WebhookEventCount",
    "WebhookEventError",
    "WebhookSurfaceHealth",
]
