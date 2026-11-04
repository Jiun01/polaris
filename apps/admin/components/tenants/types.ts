/**
 * Tenant UI types (Phase 9 PR-3).
 *
 * HAND-MAINTAINED BY DESIGN, not a TODO: re-confirmed during
 * audit-batch-f-frontend-consolidation (2026-07) that no `Tenant*`
 * Pydantic response model has been surfaced to packages/shared-types —
 * admin_tenants.py's tenant-detail/list endpoints don't export their
 * response shape there. Frontend-side shapes derived from the API's
 * tenant rows; keep in sync manually until a shared-types model exists.
 * Re-check packages/shared-types/python/polaris_shared_types/ before
 * adding a new hand-maintained duplicate elsewhere.
 */

export type TenantStatus = "active" | "suspended" | "deleted";

export type SuspendedReason = null | "quota_exhausted" | "payment_overdue";

export type PlanTier = "starter" | "pro" | "ultra" | "enterprise";

export interface TenantSummary {
  id: string;
  slug: string;
  display_name: string;
  status: TenantStatus;
  suspended_reason: SuspendedReason;
  plan_tier: PlanTier | null;
  mrr_myr: number | null;
  user_count: number;
  channel_count: number;
  last_inbound_at: string | null; // RFC3339 UTC
  created_at: string;
}

export interface TenantDetail extends TenantSummary {
  /** Primary (earliest 'admin') tenant user — target for impersonation. */
  primary_user_id: string | null;
  primary_user_email: string | null;
  contact_email: string | null;
  contact_phone: string | null;
  business_name: string | null;
  ssm: string | null;
  vertical: string | null;
  timezone: string | null;
  /** RFC3339 UTC timestamp set when status flips to 'deleted'. */
  deleted_at: string | null;
  /** Chatwoot account ID — required for inbox lookup and email config. */
  chatwoot_account_id: number | null;
  /** Easy!Appointments account state (settings.ea_account), when provisioned. */
  ea_account: EaAccountStub | null;
  channels: TenantChannelStub[];
  recent_audit: AuditExtractStub[];
}

/** EA account state mirrored from tenants.settings.ea_account. */
export interface EaAccountStub {
  status: "active" | "deprovisioned";
  admin_user_id?: number | null;
  provider_id?: number | null;
  service_id?: number | null;
  email?: string | null;
}

/**
 * Minimal channel info for the tenant detail page.
 *
 * `kind` is a 6-value union (5 canonical entitlement kinds +
 * legacy "instagram") by design, not drift to fix by deletion — see
 * ../docs/internal-ops/PRPs/plans/completed/audit-batch-f-frontend-consolidation.plan.md
 * Decision #2. `_CANONICAL_CHANNEL_KINDS` (admin_tenants.py) is the
 * *entitlement* set (5 kinds, `instagram_login` only), but rows in
 * `tenant_channels` can still be the legacy FB-page `instagram` kind for
 * tenants provisioned before the Instagram Login migration — this detail
 * view must render old rows without crashing. Superset-for-display, not
 * an enum to narrow.
 */
export interface TenantChannelStub {
  id: string;
  kind:
    | "whatsapp"
    | "instagram"
    | "instagram_login"
    | "messenger"
    | "telegram"
    | "email";
  inbox_name: string | null;
  connected: boolean;
  status: string;
  chatwoot_inbox_id: number | null;
  waba_id: string | null;
  phone_number_id: string | null;
  fb_page_id: string | null;
  ig_business_account_id: string | null;
  wa_messaging_tier: string | null;
  mm_lite_status: string | null;
  telegram_bot_username: string | null;
  /** Masked last-4 of the outbound access token (e.g. "••••3a2f"), or null if not set. Never the full token. */
  access_token_last4: string | null;
  created_at?: string | null;
}

export interface AuditExtractStub {
  id: string;
  occurred_at: string;
  action: string;
  actor_label: string;
}
