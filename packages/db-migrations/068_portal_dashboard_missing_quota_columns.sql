-- Migration 068 — Add missing tenant_quotas tracking columns for portal dashboard.
--
-- Phase 8 tenant_dashboard.py was written expecting columns that were never
-- added to the migration: knowledge_chunks_total_mb_used,
-- knowledge_chunks_total_mb_cap, broadcasts_sent_this_period, and
-- bookings_made_this_period. The existing knowledge_chunks_total_mb column
-- holds the cap value; this migration preserves it and aliases it into the
-- expected _cap column so both column names work.
--
-- Callers: apps/api/polaris_api/routers/tenant_dashboard.py _QUOTA_SQL
-- Rollback (local only):
--   ALTER TABLE tenant_quotas
--     DROP COLUMN IF EXISTS knowledge_chunks_total_mb_used,
--     DROP COLUMN IF EXISTS knowledge_chunks_total_mb_cap,
--     DROP COLUMN IF EXISTS broadcasts_sent_this_period,
--     DROP COLUMN IF EXISTS bookings_made_this_period;

-- Knowledge usage counter (MB stored, updated by ingestion worker).
ALTER TABLE tenant_quotas
  ADD COLUMN IF NOT EXISTS knowledge_chunks_total_mb_used INT NOT NULL DEFAULT 0;

COMMENT ON COLUMN tenant_quotas.knowledge_chunks_total_mb_used IS
  'Total MB of knowledge chunks currently stored for this tenant. '
  'Updated by the ingestion worker on each ingest/delete cycle.';

-- Knowledge cap column — mirrors knowledge_chunks_total_mb so the dashboard
-- SQL can refer to a consistently-named _cap column.
ALTER TABLE tenant_quotas
  ADD COLUMN IF NOT EXISTS knowledge_chunks_total_mb_cap INT NOT NULL DEFAULT 0;

-- Populate cap from the existing knowledge_chunks_total_mb for existing rows.
UPDATE tenant_quotas
   SET knowledge_chunks_total_mb_cap = knowledge_chunks_total_mb
 WHERE knowledge_chunks_total_mb_cap = 0
   AND knowledge_chunks_total_mb > 0;

COMMENT ON COLUMN tenant_quotas.knowledge_chunks_total_mb_cap IS
  'Maximum MB of knowledge chunks allowed for this tenant. '
  'Mirrors knowledge_chunks_total_mb; kept as a separate column for '
  'symmetry with the _used column and the dashboard SQL contract.';

-- Broadcast counter (resets each billing cycle by Phase 10 job).
ALTER TABLE tenant_quotas
  ADD COLUMN IF NOT EXISTS broadcasts_sent_this_period INT NOT NULL DEFAULT 0;

COMMENT ON COLUMN tenant_quotas.broadcasts_sent_this_period IS
  'Number of broadcasts sent since the start of the current billing period. '
  'Incremented by the broadcast worker; reset by the billing cycle job.';

-- Booking counter (resets each billing cycle by Phase 10 job).
ALTER TABLE tenant_quotas
  ADD COLUMN IF NOT EXISTS bookings_made_this_period INT NOT NULL DEFAULT 0;

COMMENT ON COLUMN tenant_quotas.bookings_made_this_period IS
  'Number of bookings made since the start of the current billing period. '
  'Incremented by the booking flow; reset by the billing cycle job.';

INSERT INTO schema_migrations (filename) VALUES ('068_portal_dashboard_missing_quota_columns.sql')
  ON CONFLICT DO NOTHING;
