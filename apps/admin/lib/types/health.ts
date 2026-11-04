/**
 * Shared TypeScript types for the System Health page (Phase 9 PR-6;
 * enriched 2026-06-01).
 *
 * Re-exported from @polaris/shared-types, generated from the Pydantic SSOT
 * at packages/shared-types/python/polaris_shared_types/platform_health.py.
 * Confirmed a true 1:1 match during audit-batch-f-frontend-consolidation
 * (2026-07): admin_health.py's GET /api/admin/health uses HealthSnapshot
 * directly as response_model, all 18 models imported from
 * polaris_shared_types.platform_health.
 *
 * GOTCHA: QuotaSaturation.top and WebhookEventBreakdown.by_status /
 * by_error_class / recent are Pydantic `Field(default_factory=list)` —
 * JSON Schema (correctly) marks these optional since they have a default,
 * so the generated types carry `?:`. They are ALWAYS present on the wire
 * (never actually omitted — default_factory populates them at
 * construction, and FastAPI always serializes the field). Bridge with
 * `?? []` at call sites rather than treating them as truly absent.
 */

export type {
  ActiveTenants,
  ApmErrors,
  BroadcastQueueDepth,
  EmailDraftQueue,
  HealthSnapshot,
  KnowledgeGapCaptures,
  LlmTokenBurn,
  MessagingVolume,
  MetaRateLimitHeadroom,
  QuotaSaturation,
  QuotaSaturationTenant,
  RecentFailures,
  RedisStatus,
  ServiceConnectivity,
  SupabaseStatus,
  WebhookEventBreakdown,
  WebhookEventCount,
  WebhookEventError,
  WebhookSurfaceHealth,
} from "@polaris/shared-types";
