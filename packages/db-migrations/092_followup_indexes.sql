-- Migration 092 — follow-up hot-path indexes.
-- Phase 13 / follow-ups PR-1 (review remediation).
--
-- Covers the scheduler + worker queries added in 091:
--   * followup_jobs.tenant_id — the worker's JOIN tenants + cancel's
--     (tenant_id, conversation_id) filter (the FK had no index).
--   * audience_members (tenant_id, phone_number_e164) — the opt-out
--     correlated subquery in both the scheduler gather + the worker fetch
--     (runs per row; existing indexes key on channel_user_id, not
--     phone_number_e164).
--   * messages (conversation_id, created_at DESC) WHERE role='user' —
--     the "last inbound" MAX(created_at) subquery (the existing composite
--     leads with tenant_id, which these subqueries don't constrain).
--
-- Plain CREATE INDEX (matches the repo's migration style + apply.py's
-- per-file transaction). For a large prod messages/audience table, run the
-- equivalent CONCURRENTLY out-of-band before applying in prod.
--
-- Rollback (local dev only):
--   DROP INDEX IF EXISTS followup_jobs_tenant_idx;
--   DROP INDEX IF EXISTS audience_members_tenant_phone_idx;
--   DROP INDEX IF EXISTS messages_conv_user_created_idx;

CREATE INDEX IF NOT EXISTS followup_jobs_tenant_idx
  ON followup_jobs (tenant_id);

CREATE INDEX IF NOT EXISTS audience_members_tenant_phone_idx
  ON audience_members (tenant_id, phone_number_e164)
  WHERE phone_number_e164 IS NOT NULL;

CREATE INDEX IF NOT EXISTS messages_conv_user_created_idx
  ON messages (conversation_id, created_at DESC)
  WHERE role = 'user';

INSERT INTO schema_migrations (filename) VALUES ('092_followup_indexes.sql')
ON CONFLICT (filename) DO NOTHING;
