"""Polaris v2 FastAPI app entrypoint.

Phase 0: lifespan configures logging + enforces the POLARIS_KMS_KEY gate for
non-local environments. Phase 1+ opens asyncpg + redis pools inside the
lifespan context manager.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from polaris_api.config import Settings, get_settings
from polaris_api.db.pools import close_pools, get_anon_pool, get_service_pool, init_pools
from polaris_api.http_client import close_http_client, init_http_client
from polaris_api.observability import configure_observability
from polaris_api.redis_pool import close_redis, get_redis, init_redis
from polaris_api.routers.admin_audit import router as admin_audit_router
from polaris_api.routers.admin_billing import router as admin_billing_router
from polaris_api.routers.admin_cost import router as admin_cost_router
from polaris_api.routers.admin_demos import router as admin_demos_router
from polaris_api.routers.admin_ea import router as admin_ea_router
from polaris_api.routers.admin_financials import router as admin_financials_router
from polaris_api.routers.admin_health import router as admin_health_router
from polaris_api.routers.admin_impersonation import router as admin_impersonation_router
from polaris_api.routers.admin_ingestion import router as admin_ingestion_router
from polaris_api.routers.admin_plan_changes import router as admin_plan_changes_router
from polaris_api.routers.admin_promotions import router as admin_promotions_router
from polaris_api.routers.admin_quotas import router as admin_quotas_router
from polaris_api.routers.admin_quotes import router as admin_quotes_router
from polaris_api.routers.admin_secrets import router as admin_secrets_router
from polaris_api.routers.admin_tenant_channels import (
    router as admin_tenant_channels_router,
)
from polaris_api.routers.admin_tenant_email import router as admin_tenant_email_router
from polaris_api.routers.admin_tenants import router as admin_tenants_router
from polaris_api.routers.booking_redirect import router as booking_redirect_router
from polaris_api.routers.health import router as health_router
from polaris_api.routers.public_audience_optout import (
    router as public_audience_optout_router,
)
from polaris_api.routers.tenant_audience import router as tenant_audience_router
from polaris_api.routers.tenant_bookings import router as tenant_bookings_router
from polaris_api.routers.tenant_branches import router as tenant_branches_router
from polaris_api.routers.tenant_broadcasts import router as tenant_broadcasts_router
from polaris_api.routers.tenant_channels import router as tenant_channels_router
from polaris_api.routers.tenant_conversations import (
    router as tenant_conversations_router,
)
from polaris_api.routers.tenant_dashboard import router as tenant_dashboard_router
from polaris_api.routers.tenant_followups import (
    router as tenant_followups_router,
)
from polaris_api.routers.tenant_knowledge import router as tenant_knowledge_router
from polaris_api.routers.tenant_knowledge_gaps import (
    router as tenant_knowledge_gaps_router,
)
from polaris_api.routers.tenant_messenger_profile import (
    router as tenant_messenger_profile_router,
)
from polaris_api.routers.tenant_notifications import (
    router as tenant_notifications_router,
)
from polaris_api.routers.tenant_plan_change_requests import (
    router as tenant_plan_change_requests_router,
)
from polaris_api.routers.tenant_privacy import router as tenant_privacy_router
from polaris_api.routers.tenant_products import (
    router as tenant_products_router,
)
from polaris_api.routers.tenant_promotions import (
    router as tenant_promotions_router,
)
from polaris_api.routers.tenant_settings import router as tenant_settings_router
from polaris_api.routers.tenant_templates import router as tenant_templates_router
from polaris_api.routers.tenant_widget import router as tenant_widget_router
from polaris_api.routers.website_leads import router as admin_leads_router
from polaris_api.routers.widget_public import router as widget_public_router
from polaris_api.scheduled.audit_chain_verify import register_scheduler, shutdown_scheduler
from polaris_api.scheduled.cost_ceiling_check import (
    register_cost_ceiling_scheduler,
    shutdown_cost_ceiling_scheduler,
)
from polaris_api.scheduled.ea_settings_sync import (
    register_ea_settings_sync_scheduler,
    shutdown_ea_settings_sync_scheduler,
)
from polaris_api.scheduled.invoice_overdue import (
    register_overdue_scheduler,
    shutdown_overdue_scheduler,
)
from polaris_api.scheduled.mrr_rollup_job import (
    register_mrr_scheduler,
    shutdown_mrr_scheduler,
)
from polaris_api.scheduled.quota_flush import (
    register_quota_flush_scheduler,
    shutdown_quota_flush_scheduler,
)
from polaris_api.scheduled.reconcile_channels import (
    register_reconcile_channels_scheduler,
    shutdown_reconcile_channels_scheduler,
)
from polaris_api.scheduled.retention_sweep import (
    register_retention_sweep_scheduler,
    shutdown_retention_sweep_scheduler,
)
from polaris_api.scheduled.weekly_digest import (
    register_weekly_digest_scheduler,
    shutdown_weekly_digest_scheduler,
)
from polaris_api.services.email_assist_worker import run_email_assist_worker
from polaris_api.services.followups import worker as followup_worker
from polaris_api.services.inbound_queue import _logging_consumer
from polaris_api.services.inbound_worker import run_worker as run_inbound_pipeline_worker
from polaris_api.services.kms_sentinel import verify_sentinel
from polaris_api.services.meta_whatsapp import get_meta_whatsapp_client
from polaris_api.services.pipeline import PipelineDeps
from polaris_api.services.secrets import CryptoError
from polaris_api.services.secrets import health_check as kms_health_check
from polaris_api.utils.logging import configure_logging
from polaris_api.webhooks.chatwoot_router import router as chatwoot_webhook_router
from polaris_api.webhooks.ea_router import router as ea_webhook_router
from polaris_api.webhooks.meta_template_status_router import (
    router as meta_template_status_webhook_router,
)
from polaris_api.webhooks.wa_account_router import (
    router as wa_account_webhook_router,
)
from polaris_api.webhooks.wa_status_router import (
    router as wa_status_webhook_router,
)

logger = logging.getLogger(__name__)

# Off-Meta channels M3 — resolved once; FileResponse opens the file per request.
_WIDGET_JS_PATH = Path(__file__).parent / "static" / "widget.js"


def _enforce_kms_key_for_non_local() -> None:
    """Refuse to boot in staging/prod if POLARIS_KMS_KEY isn't usable.

    Local dev is permissive (no rows exist, regen safe). Staging + prod must
    have the key set — otherwise Phase 1+ tenant_secrets writes/reads would
    raise at runtime per-tenant rather than at boot, leaking errors to clients.

    Phase 1 will extend this with a sentinel check against `platform_health`
    (Q21 resolved 2026-05-15) to detect key SWAPS — this is just the
    presence check.
    """
    settings = get_settings()
    if settings.environment == "local":
        return
    if not kms_health_check():
        raise RuntimeError(
            f"POLARIS_KMS_KEY must be set (32 bytes hex) in environment="
            f"{settings.environment}. Refusing to boot. See "
            f"../docs/Polaris V2/runbooks/ for key rotation procedure."
        )


def _enforce_chatwoot_secret_for_non_local() -> None:
    """Refuse to boot in staging/prod if the Chatwoot webhook HMAC secret is unset.

    Chatwoot-native ingress (rehaul) is the only inbound path now. The verifier
    fails closed on an empty secret, so a missing CHATWOOT_WEBHOOK_HMAC_SECRET
    makes EVERY inbound 401 — a silent total-inbound outage that looks like an
    attack in the logs rather than a misconfiguration. Catch it at boot instead.
    Local dev is permissive (HMAC is optional there).
    """
    settings = get_settings()
    if settings.environment == "local":
        return
    if not settings.chatwoot_webhook_hmac_secret:
        raise RuntimeError(
            "CHATWOOT_WEBHOOK_HMAC_SECRET must be set in environment="
            f"{settings.environment}. Refusing to boot — without it every "
            "Chatwoot inbound webhook fails HMAC verification (total inbound "
            "outage). Set it to the secret configured on the Chatwoot fork."
        )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Async lifespan — startup before yield, shutdown after.

    Phase 1 PR-2: opens both asyncpg pools (anon + service). In local dev with
    neither POLARIS_DB_URL_ANON nor POLARIS_DB_URL_SERVICE set, pool init is
    skipped so the API can still boot for non-DB routes (e.g. /v1/health).
    Production environments must have both set — `init_pools` raises if not.

    Phase 1 PR-3: after pools open, verify the KMS sentinel in platform_health.
    If the stored sentinel decrypts to the expected hash, the running
    POLARIS_KMS_KEY matches the one that wrote tenant_secrets rows. If the
    row is missing, local dev logs a warning and continues; staging/prod
    refuses to boot (forces operator to run seed_kms_sentinel.py).
    """
    configure_logging()
    settings = get_settings()
    # Phase 10 PR-2: wire Sentry + OTel before any pool/queue activity so the
    # first real request is already covered by tracing.  Both subsystems degrade
    # gracefully when DSN/endpoint are empty (local dev has neither).
    configure_observability(
        sentry_dsn=settings.sentry_dsn,
        otel_endpoint=settings.otel_endpoint,
        otel_service_name=settings.otel_service_name,
        otel_headers=settings.otel_headers,
        environment=settings.environment,
        release=settings.version,
    )
    _enforce_kms_key_for_non_local()
    _enforce_chatwoot_secret_for_non_local()
    logger.info("polaris-api starting")

    pools_initialised = False
    # Local dev convenience: if the dev hasn't run migration 010 + bootstrapped
    # the role passwords yet, allow boot without pools so /v1/health still
    # works. Staging + prod always require the DSNs (init_pools raises).
    if settings.polaris_db_url_anon and settings.polaris_db_url_service:
        await _init_pools_with_retry(settings)
        pools_initialised = True
        logger.info("asyncpg dual-pool initialised")
    elif settings.environment != "local":
        # init_pools' own error is clearer than a vague "you forgot env vars";
        # let it raise to surface which DSN is missing.
        await _init_pools_with_retry(settings)
        pools_initialised = True

    if pools_initialised:
        await _verify_kms_sentinel(settings.environment)
        # Phase 9 PR-5: register nightly audit chain verifier.
        register_scheduler(get_service_pool())
        # Phase 9 PR-8: daily 09:00 MYT invoice overdue flipper.
        register_overdue_scheduler(get_service_pool())
        # Phase 9 PR-11: nightly 02:00 MYT MRR rollup + one-shot Sentry tripwire.
        register_mrr_scheduler(get_service_pool())
        # Phase 9 PR-11: Monday 09:00 MYT weekly digest email to operator.
        register_weekly_digest_scheduler(get_service_pool())
        # Phase 10 PR-4: nightly 03:00 MYT per-tenant cost ceiling check +
        # platform-wide spend alert.  Requires service pool + Mimir config.
        # Mimir absent → job logs a single info line and exits early; no
        # false alerts fire on misconfigured environments.
        register_cost_ceiling_scheduler(get_service_pool())
        # System Health Meta tile (migration 085): persist Meta's
        # X-Business-Use-Case-Usage rate-limit header from every WhatsApp send.
        # Best-effort sink — acquires its own service-pool conn and swallows
        # all errors so telemetry never affects the send path.
        from polaris_api.services.meta_usage import record_usage as _meta_record_usage
        from polaris_api.services.meta_whatsapp import (
            set_usage_sink as _set_meta_usage_sink,
        )

        _set_meta_usage_sink(_meta_record_usage)

    # Phase 2 PR-1: open the Redis singleton. Lazy connect — no PING here
    # because /v1/health should still answer when Redis is down so an
    # operator can read the health endpoint to diagnose.
    redis_initialised = False
    if settings.redis_url:
        await init_redis()
        redis_initialised = True
        logger.info("redis client initialised")
    elif settings.environment != "local":
        # init_redis' own error is clearer than vague boot failure.
        await init_redis()
        redis_initialised = True

    # audit Batch B (B6): open the shared httpx.AsyncClient singleton right
    # after Redis — every hot-path outbound call (meta/chatwoot/booking/
    # media_download) prefers this over opening a fresh AsyncClient (new TLS
    # handshake) per call. Always initialised (no settings gate — httpx needs
    # no external DSN/URL to construct), mirroring init_pools/init_redis'
    # idempotency-guarded singleton shape.
    await init_http_client()
    http_client_initialised = True
    logger.info("shared http client initialised")

    # Phase 10 PR-1: nightly 00:05 MYT bot-replies Redis → tenant_usage flush.
    # Requires both service pool (cross-tenant writes) and Redis (counter scan).
    if pools_initialised and redis_initialised:
        register_quota_flush_scheduler(get_service_pool(), get_redis())

    # Phase 10 PR-3: nightly 02:00 MYT retention sweep + DSAR grace-period
    # purge.  Runs after quota_flush (00:05) and before audit_chain_verify
    # (04:00) — no overlap risk.  Service pool only (no Redis).
    if pools_initialised:
        register_retention_sweep_scheduler(get_service_pool())

    # Phase 8 follow-on: hourly EA → Polaris booking-settings mirror so the bot
    # enforces the tenant's current Calendar-admin policy + schedule. Service
    # pool only; per-tenant fail-soft (EA down → tenant skipped, batch continues).
    if pools_initialised:
        register_ea_settings_sync_scheduler(get_service_pool())

    # Silent-failure audit fix (A5, 2026-07-02): hourly self-heal for
    # unattached agent-bots + missing channel access-token secrets. Service
    # pool only; per-channel fail-soft (one bad Chatwoot account never blocks
    # the rest of the batch).
    if pools_initialised:
        register_reconcile_channels_scheduler(get_service_pool())

    # Phase 4 PR-4: start the LLM pipeline consumer when deps are ready
    # (pools + Redis + Meta token), else fall back to the Phase 2
    # logging consumer so local dev without a full Phase 4 env still
    # gets a working queue drain. Selection happens here once at boot;
    # there's no hot-swap.
    consumer_task = _start_inbound_consumer(
        settings=settings,
        pools_initialised=pools_initialised,
        redis_initialised=redis_initialised,
    )

    # Phase 13 follow-ups: in-process tick worker draining due AI follow-ups
    # (service pool, cross-tenant). Mirrors the inbound consumer's lifecycle.
    followup_task = (
        asyncio.create_task(followup_worker.run_forever(), name="followup-worker")
        if pools_initialised
        else None
    )

    # Email channel M2: in-process worker draining the email-assist queue
    # (service pool, cross-tenant). Needs pools + Redis for RAG / LLM / quota.
    email_assist_task = (
        asyncio.create_task(run_email_assist_worker(), name="email-assist-worker")
        if pools_initialised and redis_initialised
        else None
    )

    try:
        yield
    finally:
        # Cancel the consumer first — once it's drained / cancelled, no
        # further DB / Redis activity is queued, so we can close those
        # downstream resources safely.
        consumer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer_task
        if followup_task is not None:
            followup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await followup_task
        if email_assist_task is not None:
            email_assist_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await email_assist_task
        shutdown_scheduler()
        shutdown_overdue_scheduler()
        shutdown_mrr_scheduler()
        shutdown_weekly_digest_scheduler()
        shutdown_quota_flush_scheduler()
        shutdown_retention_sweep_scheduler()
        shutdown_ea_settings_sync_scheduler()
        shutdown_reconcile_channels_scheduler()
        shutdown_cost_ceiling_scheduler()
        if http_client_initialised:
            await close_http_client()
            logger.info("shared http client closed")
        if redis_initialised:
            await close_redis()
            logger.info("redis client closed")
        if pools_initialised:
            await close_pools()
            logger.info("asyncpg pools closed")
        logger.info("polaris-api stopping")


async def _init_pools_with_retry(settings: Settings) -> None:
    """Open the asyncpg pools, retrying transient connection failures.

    A Supavisor pooler blip during deploy (pooler restart / cache lag — it
    accepts the TCP connection but returns ``{:error, :econnrefused}`` for the
    upstream DB) would otherwise raise out of ``init_pools`` and crash the
    lifespan → uvicorn exits → container restart loop. We retry with
    exponential backoff so a momentary outage self-heals; a sustained outage
    still fails loudly once attempts are exhausted.

    A ``RuntimeError`` from ``init_pools`` (unset DSN / double-start) is a
    config/programming error, not a transient connection failure — re-raise it
    immediately rather than burning retries. ``init_pools`` refuses to run when
    a pool is already open, so we drain any half-opened pool with
    ``close_pools`` before each retry.
    """
    attempts = settings.db_pool_init_attempts
    base_delay = settings.db_pool_init_base_delay_s
    for attempt in range(1, attempts + 1):
        try:
            await init_pools()
            return
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 — any pooler/socket error is retryable
            await close_pools()  # reset half-opened state for a clean retry
            if attempt >= attempts:
                logger.error("init_pools failed after %d attempt(s); giving up", attempts)
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "init_pools attempt %d/%d failed (%s); retrying in %.1fs",
                attempt,
                attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)


def _start_inbound_consumer(
    *,
    settings: object,
    pools_initialised: bool,
    redis_initialised: bool,
) -> asyncio.Task[None]:
    """Pick Phase 4 pipeline worker vs Phase 2 logging consumer.

    Pipeline worker requires:
      - asyncpg anon pool (for the orchestrator's RLS-scoped
        transaction)
      - Redis (for conversation hot cache + tier cache)
      - A real ANTHROPIC_API_KEY (else every message would log
        warnings and ACK without doing anything useful — better to
        loudly fall back to the logging consumer and surface the
        misconfig in the boot log)

    Anything missing → keep the Phase 2 logging consumer so
    /v1/health works and the queue still drains. The boot log line
    tells the operator which one they got.
    """
    anthropic_key = getattr(settings, "anthropic_api_key", "")
    has_real_anthropic = anthropic_key and not anthropic_key.startswith("placeholder-")
    llm_stub = getattr(settings, "llm_stub_enabled", False)
    if pools_initialised and redis_initialised and (has_real_anthropic or llm_stub):
        deps = PipelineDeps(
            anon_pool=get_anon_pool(),
            redis=get_redis(),
            meta_client=get_meta_whatsapp_client(),
        )
        logger.info("inbound_worker: Phase 4 pipeline consumer selected")
        return asyncio.create_task(
            run_inbound_pipeline_worker(deps),
            name="inbound_queue_pipeline_consumer",
        )
    logger.info(
        "inbound_worker: falling back to Phase 2 logging consumer "
        "(pools=%s redis=%s anthropic_real=%s)",
        pools_initialised,
        redis_initialised,
        has_real_anthropic,
    )
    return asyncio.create_task(
        _logging_consumer(),
        name="inbound_queue_logging_consumer",
    )


async def _verify_kms_sentinel(environment: str) -> None:
    """Boot-time KMS fingerprint check (PR-3, ADR-on-deck).

    Decrypts the platform_health sentinel with the running POLARIS_KMS_KEY
    and compares SHA-256(plaintext) against the stored hash.

    Behaviour:
      - sentinel matches → log + continue (the common path)
      - sentinel mismatch → CryptoError → re-raise as RuntimeError to abort
        startup. Mismatch means the key was rotated WITHOUT re-encrypting
        existing tenant_secrets rows, which would silently corrupt every
        subsequent write under the wrong key.
      - sentinel row absent + environment != local → RuntimeError. Forces
        operator to run apps/api/scripts/seed_kms_sentinel.py.
      - sentinel row absent + environment == local → warn + continue, so
        a fresh dev DB doesn't require an extra seed step before /v1/health
        works.
    """
    try:
        async with get_service_pool().acquire() as conn:
            seeded = await verify_sentinel(conn)
    except CryptoError as exc:
        raise RuntimeError(
            f"KMS sentinel verification failed: {exc}. The running "
            "POLARIS_KMS_KEY does not match the key used to seed "
            "platform_health. Refusing to boot — see ../docs/internal-ops/RUNBOOK.md §2b."
        ) from exc

    if seeded:
        logger.info("kms sentinel verified")
        return
    if environment != "local":
        raise RuntimeError(
            "platform_health sentinel row is missing in environment="
            f"{environment}. Run apps/api/scripts/seed_kms_sentinel.py "
            "before booting. See ../docs/internal-ops/RUNBOOK.md §2b."
        )
    logger.warning(
        "kms sentinel not seeded (local dev — skipping). "
        "Run apps/api/scripts/seed_kms_sentinel.py to populate."
    )


app = FastAPI(
    title="Polaris v2 API",
    description="Multi-tenant chatbot SaaS for SMEs. Phase 0 ships /v1/health only.",
    version="0.0.0",
    lifespan=lifespan,
)
# /v1 prefix on all routers so Phase 1+ versioning is set up day one. URL
# stability matters once external integrations (webhooks, customer-facing
# embeds) start pointing at the API.
app.include_router(health_router, prefix="/v1")
# v2/phase-13: public short booking-link redirect — unauthenticated, scoped by jti.
app.include_router(booking_redirect_router)
# Chatwoot-native rehaul: meta_router retired — Meta posts to Chatwoot now;
# non-message webhooks reach Polaris via the fork-relay → wa_status /
# meta_template_status / wa_account routers below.
# Phase 2 PR-5: Chatwoot Agent-Bot loopback dispatcher. Telegram inbound also
# rides this agent-bot path now (native Chatwoot Channel::Telegram → resolve_chatwoot
# channel_kind 'telegram'); the legacy own-webhook telegram_router was deleted.
app.include_router(chatwoot_webhook_router, prefix="/v1/webhooks")
# Off-Meta channels M3 — public website-widget config + redirect.
app.include_router(widget_public_router, prefix="/api/v1/widget")


@app.get("/widget.js", include_in_schema=False)
async def serve_widget_js() -> FileResponse:
    """Serve the launcher-bubble web component (off-Meta channels M3).

    Embedded cross-origin via <script src=".../widget.js">; long-cache + permissive
    CORS since it's static, public, non-sensitive JS.
    """
    return FileResponse(
        _WIDGET_JS_PATH,
        media_type="application/javascript",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=3600",
        },
    )


# Phase 3 PR-1: Knowledge ingestion intake + admin chunking-config
# (per Q-PHASE3-06 prefix layering: /v1/webhooks/* + /api/tenant/* + /api/admin/*).
app.include_router(tenant_knowledge_router, prefix="/api/tenant/knowledge")
# Phase 6 PR-3: WhatsApp template authoring + Meta lifecycle webhook.
app.include_router(tenant_templates_router, prefix="/api/tenant/templates")
app.include_router(meta_template_status_webhook_router, prefix="/v1/webhooks")
# Phase 6 PR-7: WhatsApp delivery status + MM Lite onboarding webhooks.
app.include_router(wa_status_webhook_router, prefix="/v1/webhooks")
app.include_router(wa_account_webhook_router, prefix="/v1/webhooks")
# v2.1: EA (Easy!Appointments fork) appointment confirm webhook → /v1/webhooks/ea.
app.include_router(ea_webhook_router, prefix="/v1/webhooks")
# Phase 6 PR-4: Audience CSV importer + PDPA-blocking opt-out viewer.
# The /api/v1/audience/opted-out endpoint is the LONE GA exception to the
# "no /api/v1/* at GA" rule (ADR-035 Am evening) — auth is portal JWT,
# not tenant API key, but the path is registered now for v3.0 stability.
app.include_router(tenant_audience_router, prefix="/api/tenant/audience")
app.include_router(public_audience_optout_router, prefix="/api/v1/audience")
# Phase 8 PR-8: tenant booking-config (lives in tenants.settings JSONB;
# EA fork reads the same blob at runtime). No new table.
app.include_router(tenant_bookings_router, prefix="/api/tenant/bookings")
# Phase 13 follow-ups: AI re-engagement toggle (tenants.settings.followup_config).
app.include_router(tenant_followups_router, prefix="/api/tenant/followups")
# Phase 8 PR-9: Promotions CRUD (universal across tiers per ADR-042) +
# Branches CRUD (Pro+ tier-gated UI-side; backend RLS uniform).
app.include_router(tenant_promotions_router, prefix="/api/tenant/promotions")
app.include_router(tenant_branches_router, prefix="/api/tenant/branches")
# Phase 15 PR-2: Product catalog CRUD + image gallery (universal across tiers
# per ADR-061; products are RAG-searchable + photo-sendable by the bot).
app.include_router(tenant_products_router, prefix="/api/tenant/products")
# Phase 8 PR-10: plan-change request flow (Phase 10 admin approves at GA;
# operator manually flips status until then per phase risk register).
app.include_router(
    tenant_plan_change_requests_router,
    prefix="/api/tenant/plan-change-requests",
)
# Phase 6 PR-5: Broadcast authoring API (create/schedule/pause/resume/cancel).
# Worker that actually sends lives in PR-6.
app.include_router(tenant_broadcasts_router, prefix="/api/tenant/broadcasts")
# Phase 8 PR-3: Dashboard aggregate read (one round-trip for the portal
# landing screen, p95 budget ≤ 400 ms). All reads RLS-scoped via the
# polaris_anon connection.
app.include_router(tenant_dashboard_router, prefix="/api/tenant/dashboard")
# Knowledge Gap Detection M3: Ultra-only ranked gap themes for the portal
# Knowledge Gaps screen. RLS-scoped; 403 KNOWLEDGE_GAPS_TIER_BLOCKED below Ultra.
app.include_router(tenant_knowledge_gaps_router, prefix="/api/tenant/knowledge-gaps")
# Phase 8 PR-5: Tenant Channels read-only list for the portal Channels
# screen. Connect flow remains admin-driven at GA per ADR-012 +
# ADR-034 Am.3 (paste-only/sales-led). Self-serve POST lands when
# Embedded Signup unblocks at v3.0.
app.include_router(tenant_channels_router, prefix="/api/tenant/channels")
# Off-Meta channels M4 — tenant website-widget config (self-serve, all tiers).
app.include_router(tenant_widget_router, prefix="/api/tenant/widget")
# Phase 8 PR-4: Tenant Settings — sectioned form backing for the
# portal's /settings screen (bot persona, business hours, handoff
# messages, AI disclosure).
app.include_router(tenant_settings_router, prefix="/api/tenant/settings")
# Phase 7 PR-5: Messenger Profile push (icebreakers / persistent_menu / get_started).
# Mounted under channels/{channel_id} so the path scopes to a single FB Page.
app.include_router(
    tenant_messenger_profile_router,
    prefix="/api/tenant/channels/{channel_id}/messenger-profile",
)
# Phase 8 PR-1: tenant notification feed (bell-icon + full-page view).
# Polled every 30 s by the portal while the bell is visible (Q-PHASE8-08).
# Backing rows are written by side-effect handlers in other phases
# (template approvals, broadcast completion, billing) through polaris_service;
# this router only exposes read + mark-read paths via polaris_anon.
app.include_router(tenant_notifications_router, prefix="/api/tenant/notifications")
# Phase 8 PR-6: read-only Conversations viewer (#2) for the portal.
# Reply surface stays in Chatwoot per ADR-030 — this router exposes
# list + detail GETs only. RLS on conversations + messages keeps the
# tenant scope.
app.include_router(
    tenant_conversations_router,
    prefix="/api/tenant/conversations",
)
app.include_router(admin_ingestion_router, prefix="/api/admin")
# Phase 9 PR-3: admin tenant lifecycle (create wizard, suspend/unsuspend,
# soft-delete with DSAR audit per ADR-029).
app.include_router(admin_tenants_router, prefix="/api/admin/tenants")
# Batch E (task E4): channel add/remove/telegram-connect + email inbox config,
# split out of admin_tenants.py — same prefix, route paths unchanged.
app.include_router(admin_tenant_channels_router, prefix="/api/admin/tenants")
app.include_router(admin_tenant_email_router, prefix="/api/admin/tenants")
# ADR-066: Demo System — CRUD over hidden demo tenants + reset-visitor + logs.
app.include_router(admin_demos_router, prefix="/api/admin/demos")
# WS4: read-only platform-wide promotions mirror for the admin /promotions page.
app.include_router(admin_promotions_router, prefix="/api/admin/promotions")
# Phase 9 PR-4: admin quota usage/override + secrets rotation with audit trail.
app.include_router(admin_quotas_router, prefix="/api/admin/quotas")
app.include_router(admin_secrets_router, prefix="/api/admin/secrets")
# v2.1: EA provider/service write-back sync onto tenant branches.
app.include_router(admin_ea_router, prefix="/api/admin/tenants")
# Phase 9 PR-5: audit log viewer + chain verifier + impersonation.
app.include_router(admin_audit_router, prefix="/api/admin/audit")
app.include_router(admin_impersonation_router, prefix="/api/admin")
# Phase 9 PR-6: System health snapshot — 5-tile dashboard with auto-refresh.
app.include_router(admin_health_router, prefix="/api/admin")
# Phase 9 PR-7/8/9: Financial suite — billing, plan-change approval, sales quotes.
app.include_router(admin_billing_router, prefix="/api/admin/billing")
app.include_router(admin_plan_changes_router, prefix="/api/admin/plan-changes")
app.include_router(admin_quotes_router, prefix="/api/admin/quotes")
# Phase 9 PR-10: per-tenant cost dashboard — Mimir proxy + amber/red banding.
app.include_router(admin_cost_router, prefix="/api/admin/cost")
# Phase 9 PR-11: MRR dashboard.
app.include_router(admin_financials_router, prefix="/api/admin/financials")
# Website leads — marketing-site form submissions (GAS → Cloudflare Function + Supabase).
app.include_router(admin_leads_router, prefix="/api/admin/leads")
# Phase 10 PR-3: DSAR endpoints — export request + status poll + self-service
# account deletion (30-day grace per ADR-029).
app.include_router(tenant_privacy_router, prefix="/api/tenant/privacy")

# E2E seed/teardown router — only mounted when POLARIS_E2E_INTERNAL_SECRET is set.
# Never runs in staging/prod (secret is unset in those environments).
_env = (get_settings().environment or "").lower()
_e2e_secret = get_settings().polaris_e2e_internal_secret
if _e2e_secret:
    # Fail closed: an accidentally-copied .env must never expose the seed/teardown
    # + mint-tenant-jwt surface in a real environment. Crash on boot instead.
    if _env in ("prod", "production", "staging"):
        raise RuntimeError(
            f"POLARIS_E2E_INTERNAL_SECRET is set but ENVIRONMENT={_env!r} — refusing "
            "to mount the /internal/e2e router outside local/CI."
        )
    from polaris_api.e2e.router import router as _internal_e2e_router  # noqa: PLC0415

    app.include_router(_internal_e2e_router, prefix="/internal/e2e")
    import logging as _logging  # noqa: PLC0415

    _logging.getLogger(__name__).warning(
        "[startup] /internal/e2e/* seed router ACTIVE — only safe in local/CI environments"
    )

# Meta Graph API mock router — only mounted when POLARIS_META_MOCK_ENABLED=true.
# Captures all POST /{version}/... calls and returns synthetic success responses.
# The polaris-api reaches this when META_API_BASE_URL=http://localhost:8000/meta-mock.
_meta_mock_enabled = get_settings().meta_mock_enabled
if _meta_mock_enabled:
    if _env in ("prod", "production", "staging"):
        raise RuntimeError(
            f"POLARIS_META_MOCK_ENABLED is true but ENVIRONMENT={_env!r} — refusing "
            "to mount the meta-mock capture router outside local/CI."
        )
    from polaris_api.routers.meta_mock import router as _meta_mock_router  # noqa: PLC0415

    app.include_router(_meta_mock_router, prefix="/meta-mock")
    import logging as _logging2  # noqa: PLC0415

    _logging2.getLogger(__name__).warning(
        "[startup] /meta-mock capture router ACTIVE — only safe in local/CI environments"
    )
