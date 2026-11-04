-- Migration 114 — Demo System v3 data model (text-gated, single-account roleplay).
-- Ref: ../docs/internal-ops/PRPs/plans/demo-system-v3.plan.md (supersedes the old
--      demo=tenant plan and ADR-027/038/039/040/046 — see the new ADR).
-- Anchors: migration 002 (tenants: demo_code, settings, system_prompt already
--          exist), migration 008b (demo_sessions table + polaris-internal seed).
--
-- This migration REUSES — does NOT recreate — the demo_sessions table and the
-- polaris-internal tenant seeded by 008b. It only adds the v3 columns, one
-- tracking column, an exact-match index, and a backfill.
--
-- Model A (hidden tenant per demo): each demo brand is a hidden tenants row
-- (is_demo = true) the operator owns; content (KB / persona / products) isolates
-- via the existing tenant-scoped RLS. polaris-internal is the GATEWAY tenant
-- (owns the operator's live channels + the default Polaris sales KB).
--
--   - is_demo: brand demos AND the polaris-internal gateway are both marked
--     true so a single `NOT is_demo` predicate excludes them from MRR / platform
--     metrics / the customer Tenants list. Safe for code resolution because
--     find_demo_tenant_by_code also requires `demo_code IS NOT NULL`, and
--     polaris-internal's demo_code is NULL — so the gateway never matches a code.
--   - demo_duration_hours / demo_max_messages: per-demo configurable caps
--     (replace the hardcoded 6h / 50-msg). Read at activation to populate the
--     demo_sessions row's expires_at / max_messages.
--   - demo_sessions.lead_flagged_at: set when a demo ends and the lead is flagged
--     (Demo System log marker). The existing chatwoot_* columns (008b) carry the
--     Chatwoot link for the private note.
--
-- Under v3, demo_sessions.tenant_id = the BRAND tenant (was polaris-internal in
-- the old model). No schema change for that — only a value change at insert.
-- 008b's RLS policy demo_sessions_self_all stays; demo reads/writes go via the
-- BYPASSRLS service pool (cross-tenant brand resolution).
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   ALTER TABLE tenants DROP COLUMN IF EXISTS is_demo;
--   ALTER TABLE tenants DROP COLUMN IF EXISTS demo_duration_hours;
--   ALTER TABLE tenants DROP COLUMN IF EXISTS demo_max_messages;
--   DROP INDEX IF EXISTS tenants_demo_code_lower_idx;
--   ALTER TABLE demo_sessions DROP COLUMN IF EXISTS lead_flagged_at;
--   UPDATE tenants SET is_demo = false WHERE id = '00000000-0000-0000-0000-000000000001';

-- ── tenants: demo flags + per-demo caps ──────────────────────────────────────
ALTER TABLE tenants
  ADD COLUMN IF NOT EXISTS is_demo             BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS demo_duration_hours INT     NOT NULL DEFAULT 6,
  ADD COLUMN IF NOT EXISTS demo_max_messages   INT     NOT NULL DEFAULT 50;

-- Exact-token demo-code resolution (case-insensitive), scoped to demo tenants.
CREATE INDEX IF NOT EXISTS tenants_demo_code_lower_idx
  ON tenants (lower(demo_code))
  WHERE is_demo = true;

-- Backfill the gateway so `NOT is_demo` excludes it from all customer-facing
-- aggregates. polaris-internal has no demo_code, so it never resolves as a demo.
UPDATE tenants
   SET is_demo = true
 WHERE id = '00000000-0000-0000-0000-000000000001';

-- ── demo_sessions: lead-flag marker (reuse 008b table) ───────────────────────
ALTER TABLE demo_sessions
  ADD COLUMN IF NOT EXISTS lead_flagged_at TIMESTAMPTZ NULL;

INSERT INTO schema_migrations (filename) VALUES ('114_demo_tenant_model.sql')
ON CONFLICT (filename) DO NOTHING;
