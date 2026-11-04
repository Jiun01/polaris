-- Migration 016 — GRANTs for webhook_events (Phase 2 / PR-2 follow-up).
--
-- Mirrors the Phase 1 pattern (011_secrets_grants.sql, 012_audit_grants.sql,
-- 014_phase1_pr5_grants.sql): grants live in a sibling file because the
-- roles polaris_anon / polaris_service are created by migration 010, which
-- sorts AFTER any inline grants in 015 would have executed.
--
-- polaris_anon gets SELECT + INSERT only:
--   - SELECT scoped by RLS to tenant_id = current_setting(...).
--   - INSERT scoped by the WITH CHECK policy in 015 (per-tenant write
--     path used by PR-4's `processed` / `replay_ignored` rows).
--   - No UPDATE/DELETE — the table is append-only at GA (Phase 10
--     retention sweeper runs as service_pool).
--
-- polaris_service gets ALL — webhook_resolver writes unresolved-tenant
-- rows (hmac_fail, tenant_not_found) and the Phase 9 admin reads
-- forensic queries.
--
-- Rollback (local only):
--   REVOKE ALL ON webhook_events FROM polaris_anon, polaris_service;

GRANT SELECT, INSERT  ON webhook_events  TO polaris_anon;
GRANT ALL             ON webhook_events  TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('016_webhook_events_grants.sql')
ON CONFLICT (filename) DO NOTHING;
