-- Migration 051 — Phase 8 schema additions.
-- Phase 8 / PR-1. Ref: docs/Polaris V2/04i_PHASE_8_DEEP.md §3.1.
-- Anchors:
--   - Q-PHASE8-09 (notifications + plan_change_requests land together
--     in the portal phase's first PR, not pulled forward into Phase 7).
--   - 00a §6.13.1 (notification-center as a cross-cutting pattern; new
--     `notifications` table is a Phase 8 deliverable, not in 05's
--     baseline migration list).
--   - 00c §12.4 (plan-change is portal-submitted at GA; Phase 10
--     admin-side approval lands the status transitions + invoice
--     generation without further schema change).
--   - 05 §6.2 (`/api/tenant/notifications/*` + `/api/tenant/plan-change-requests/*`
--     are the consuming surfaces).
--
-- Two tables added:
--
--   (a) notifications — portal in-app feed. Drives the bell-icon
--       dropdown (30 s visibility-gated polling per Q-PHASE8-08) and
--       full-page filtered view. recipient_user_id NULL = tenant-wide
--       broadcast (template approval, broadcast completion, billing).
--       Non-NULL = single user (DSAR-ready, plan-change-approval).
--
--   (b) plan_change_requests — portal-submitted plan-change requests.
--       proration_preview is snapshotted at request time for tenant
--       transparency. Phase 10 admin approval recomputes
--       server-authoritative on decision; status fields stay NULL
--       until approval. No portal-side UPDATE of decision_* columns
--       (admin path only).
--
-- Append-only. Both tables start empty — no backfill.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   DROP TABLE IF EXISTS plan_change_requests CASCADE;
--   DROP TABLE IF EXISTS notifications CASCADE;

-- =====================================================================
-- (a) notifications
-- =====================================================================

CREATE TABLE notifications (
  id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           UUID         NOT NULL REFERENCES tenants(id)    ON DELETE CASCADE,

  -- NULL = visible to every tenant_user on the tenant (template approved,
  -- broadcast completed, billing events). Non-NULL = visible only to that
  -- user (e.g. plan-change-approved fires for the requester).
  --
  -- Targets auth.users(id) because tenant_users is a composite-PK join
  -- table with no id column. Tenant-membership check is enforced via
  -- RLS on tenant_id at SELECT/INSERT/UPDATE time; an orphan
  -- recipient_user_id (no tenant_users row binding that user to the
  -- tenant) would still be tenant-scoped by RLS — the user just won't
  -- see it because their JWT carries a different tenant_id.
  recipient_user_id   UUID         NULL REFERENCES auth.users(id) ON DELETE CASCADE,

  -- App-side enum (no PG type) so adding a kind doesn't require a
  -- migration. Catalogue per 04i §4.1 NotificationKind:
  --   template_approved | template_rejected | template_paused | template_disabled
  --   broadcast_completed | broadcast_failed
  --   invoice_issued | invoice_overdue | invoice_paid
  --   plan_change_approved | account_deletion_grace_warning
  --   ingestion_complete | ingestion_failed | ingestion_partial
  --   quota_replies_80 | quota_replies_100
  --   mm_lite_status_changed
  --   branch_agent_invite_accepted
  --   demo_session_converted
  kind                TEXT         NOT NULL,

  title               TEXT         NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
  body                TEXT         NULL     CHECK (body IS NULL OR char_length(body) <= 2000),
  href                TEXT         NULL     CHECK (href IS NULL OR char_length(href) <= 512),

  -- Free-form payload for renderer-side detail surfacing (template_id,
  -- broadcast_id, invoice_number, etc.). Validated app-side; DB keeps it
  -- opaque so adding a notification kind doesn't require a column.
  metadata            JSONB        NOT NULL DEFAULT '{}'::jsonb,

  -- NULL = unread. read_at is per-user when recipient_user_id IS NOT NULL
  -- (only that user's view of the row carries the marker — but at GA
  -- there is one tenant_user per tenant per ADR-030, so the distinction
  -- collapses to tenant-wide). Future v2.1 tenant_operator role will
  -- require splitting this into a notifications_read_state join table;
  -- not built at GA.
  read_at             TIMESTAMPTZ  NULL,

  created_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Partial indexes: most poll-time queries filter on (tenant_id, read_at IS NULL).
-- The recipient-scoped variant is rare today (one tenant_user per tenant) but
-- becomes hot when v2.1 multi-user lands.
CREATE INDEX notifications_tenant_unread_idx
  ON notifications (tenant_id, created_at DESC)
  WHERE read_at IS NULL;

CREATE INDEX notifications_recipient_unread_idx
  ON notifications (recipient_user_id, created_at DESC)
  WHERE read_at IS NULL AND recipient_user_id IS NOT NULL;

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications FORCE  ROW LEVEL SECURITY;

-- FOR ALL covers SELECT/INSERT/UPDATE/DELETE. Tenant scope at INSERT
-- prevents cross-tenant write-through; tenant scope at UPDATE keeps
-- mark-read confined to the calling tenant's rows.
CREATE POLICY notifications_self_all ON notifications
  FOR ALL
  USING       (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK  (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- polaris_anon (portal-JWT path): SELECT + UPDATE for mark-read.
-- INSERT/DELETE not granted — only side-effect writers (broadcast
-- worker, template lifecycle handler, billing) write rows, and they
-- run through polaris_service.
GRANT SELECT, UPDATE ON notifications TO polaris_anon;
GRANT ALL             ON notifications TO polaris_service;

COMMENT ON TABLE  notifications IS
  'Phase 8 — portal in-app notification feed; bell-icon (30 s poll, tab-visibility-gated per Q-PHASE8-08) + full-page filtered view.';
COMMENT ON COLUMN notifications.recipient_user_id IS
  'NULL = tenant-wide; non-NULL = single auth.users row. RLS scopes by tenant_id regardless.';
COMMENT ON COLUMN notifications.kind IS
  'App-side enum (see polaris_shared_types.notifications.NotificationKind). New kinds do not require migration — only an app deploy.';
COMMENT ON COLUMN notifications.read_at IS
  'NULL = unread. GA has one tenant_user per tenant (ADR-030) so this is effectively tenant-wide; v2.1 multi-user will split this into a per-user read_state join.';

-- =====================================================================
-- (b) plan_change_requests
-- =====================================================================

CREATE TABLE plan_change_requests (
  id                       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                UUID         NOT NULL REFERENCES tenants(id)    ON DELETE CASCADE,
  -- requested_by_user_id is operational (not audit); cascade on user
  -- deletion matches DSAR right-to-erasure expectations per ADR-029.
  -- ON DELETE on auth.users FKs is mandatory because polaris_service-
  -- driven deletes of auth.users rows would otherwise hit FK violations.
  requested_by_user_id     UUID         NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,

  current_tier             TEXT         NOT NULL
    CHECK (current_tier IN ('starter', 'pro', 'ultra', 'enterprise')),
  target_tier              TEXT         NOT NULL
    CHECK (target_tier  IN ('starter', 'pro', 'ultra', 'enterprise')),
  current_billing_cycle    TEXT         NOT NULL
    CHECK (current_billing_cycle IN ('monthly', 'annual')),
  target_billing_cycle     TEXT         NOT NULL
    CHECK (target_billing_cycle  IN ('monthly', 'annual')),

  -- Snapshot at request time per 00c §12.4.1. Phase 10 admin path
  -- recomputes the authoritative value on approval — this column is
  -- a tenant-transparency artifact, not the source of truth.
  proration_preview        JSONB        NOT NULL DEFAULT '{}'::jsonb,

  status                   TEXT         NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),

  -- Optional tenant message; surfaces in admin approval UI.
  reason                   TEXT         NULL CHECK (reason IS NULL OR char_length(reason) <= 1000),

  -- Phase 10 admin fills these on decision. Stay NULL while status='pending'.
  -- SET NULL on user deletion: preserves the row for the requester even
  -- if the admin's auth.users row is later DSAR-deleted.
  decided_by_user_id       UUID         NULL REFERENCES auth.users(id) ON DELETE SET NULL,
  decided_at               TIMESTAMPTZ  NULL,
  decision_note            TEXT         NULL CHECK (decision_note IS NULL OR char_length(decision_note) <= 1000),

  created_at               TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX plan_change_requests_tenant_idx
  ON plan_change_requests (tenant_id, created_at DESC);

-- Pending-only partial index: admin path lists pending requests
-- platform-wide; tenant path reads its own pending state.
CREATE INDEX plan_change_requests_pending_idx
  ON plan_change_requests (created_at DESC)
  WHERE status = 'pending';

ALTER TABLE plan_change_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE plan_change_requests FORCE  ROW LEVEL SECURITY;

CREATE POLICY plan_change_requests_self_all ON plan_change_requests
  FOR ALL
  USING       (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK  (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- polaris_anon: SELECT + INSERT only. Tenants submit + read; never
-- update the decision columns. Admin path runs through polaris_service.
GRANT SELECT, INSERT ON plan_change_requests TO polaris_anon;
GRANT ALL            ON plan_change_requests TO polaris_service;

COMMENT ON TABLE  plan_change_requests IS
  'Phase 8 portal-submitted; Phase 10 admin-approved per 00c §12.4. proration_preview is the tenant-transparency snapshot; admin recomputes authoritative on approval.';
COMMENT ON COLUMN plan_change_requests.proration_preview IS
  'JSONB matching polaris_shared_types.plan_change_requests.ProrationPreview: {amount_due_myr, period_credited_myr, quota_topup_bot_replies, prorate_basis}.';

INSERT INTO schema_migrations (filename) VALUES ('051_phase8_notifications_and_plan_changes.sql')
ON CONFLICT (filename) DO NOTHING;
