-- Migration 057 — Phase 10 PR-1: tenant_usage rollup + quota columns + STOP keyword.
-- Refs: ADR-017 (bot_replies meter), ADR-026 (tier caps), 04k §2.
--
-- Four changes:
--
--   (a) tenant_quotas.plan_tier        [ALTER ADD]
--       Bug-fix: admin_tenants.py Phase 9 router reads/writes plan_tier on
--       tenant_quotas but migration 007 never added the column.  All tenant
--       creation and channel tier-cap enforcement were silently failing against
--       a real DB.  Denormalised copy of tenants.plan_tier; kept in sync by
--       application writes at provisioning + plan-change time.
--
--   (b) tenant_quotas.bot_replies_used_this_period  [ALTER ADD]
--       Column referenced in tenant_dashboard.py (Phase 8) but never created.
--       Nightly quota_flush job writes the Redis counter snapshot here so the
--       portal Usage page shows accurate totals without a Redis round-trip.
--
--   (c) tenant_usage                   [CREATE]
--       Append-only history table.  Each row = one period snapshot from the
--       nightly Redis flush.  Keeps 13 months of bot-reply totals for billing
--       reconciliation + the Phase 10 PR-4 cost dashboard.
--
--   (d) audience_members.stop_keyword_matched  [ALTER ADD]
--       Records the exact keyword string that triggered an opt-out
--       (Phase 10 PR-1 STOP handler).  NULL for non-keyword opt-outs.
--
-- Rollback (local dev only):
--   ALTER TABLE audience_members DROP COLUMN IF EXISTS stop_keyword_matched;
--   DROP TABLE IF EXISTS tenant_usage;
--   ALTER TABLE tenant_quotas DROP COLUMN IF EXISTS bot_replies_used_this_period;
--   ALTER TABLE tenant_quotas DROP COLUMN IF EXISTS plan_tier;

-- ── (a) tenant_quotas.plan_tier ──────────────────────────────────────────────

ALTER TABLE tenant_quotas
  ADD COLUMN IF NOT EXISTS plan_tier TEXT NOT NULL DEFAULT 'starter';

COMMENT ON COLUMN tenant_quotas.plan_tier IS
  'Phase 10 PR-1 bug-fix: denormalised mirror of tenants.plan_tier. '
  'Written by admin_tenants create + plan-change paths. '
  'Used by tier-cap enforcement + quota check without joining tenants.';

-- Back-fill from tenants for any rows already in the DB.
UPDATE tenant_quotas tq
   SET plan_tier = t.plan_tier::TEXT
  FROM tenants t
 WHERE tq.tenant_id = t.id
   AND tq.plan_tier = 'starter';  -- DEFAULT sentinel only, skip already-correct rows

-- ── (b) tenant_quotas.bot_replies_used_this_period ───────────────────────────

ALTER TABLE tenant_quotas
  ADD COLUMN IF NOT EXISTS bot_replies_used_this_period INT NOT NULL DEFAULT 0;

COMMENT ON COLUMN tenant_quotas.bot_replies_used_this_period IS
  'Phase 10 PR-1: snapshot of the Redis bot_replies counter for the '
  'current billing period.  Written nightly by scheduled/quota_flush.py. '
  'Read by tenant_dashboard for the Usage page.';

-- ── (c) tenant_usage rollup table ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tenant_usage (
  id            BIGSERIAL    PRIMARY KEY,
  tenant_id     UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  period_yyyymm CHAR(6)      NOT NULL,   -- e.g. '202605'
  bot_replies   BIGINT       NOT NULL DEFAULT 0,
  flushed_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
  CONSTRAINT tenant_usage_tenant_period_uniq UNIQUE (tenant_id, period_yyyymm)
);

COMMENT ON TABLE tenant_usage IS
  'Phase 10 PR-1: nightly bot-reply counter rollup from Redis. '
  'One row per (tenant, billing-period).  Written by scheduled/quota_flush.py. '
  'Used for billing reconciliation and the admin cost dashboard (PR-4).';

-- Service pool owns writes (cross-tenant flush); anon pool reads its own row.
ALTER TABLE tenant_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_usage FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_usage_self_select ON tenant_usage
  FOR SELECT
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

GRANT SELECT ON tenant_usage TO polaris_anon;
GRANT SELECT, INSERT, UPDATE ON tenant_usage TO polaris_service;
GRANT USAGE, SELECT ON SEQUENCE tenant_usage_id_seq TO polaris_service;

-- ── (d) audience_members.stop_keyword_matched ────────────────────────────────

ALTER TABLE audience_members
  ADD COLUMN IF NOT EXISTS stop_keyword_matched TEXT NULL;

COMMENT ON COLUMN audience_members.stop_keyword_matched IS
  'Phase 10 PR-1: the exact STOP keyword string matched (e.g. "STOP BOT REPLY"). '
  'NULL for opt-outs not driven by a keyword (manual, DSAR, bulk).';

-- ── schema_migrations bookkeeping ─────────────────────────────────────────────

INSERT INTO schema_migrations (filename)
VALUES ('057_phase10_tenant_usage_stop_keyword.sql')
ON CONFLICT (filename) DO NOTHING;
