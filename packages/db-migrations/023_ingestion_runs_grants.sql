-- Migration 023 — ingestion_runs grants (Phase 3 / PR-1).
-- Pairs with 022_ingestion_runs.sql.
--
-- Platform-only table — polaris_anon receives NO grants. Tenants never see
-- worker heartbeats (which would leak the existence + timing of other
-- tenants' ingestion runs). Phase 9 admin queries this via service-role.
--
-- Rollback (local only):
--   REVOKE ALL ON ingestion_runs FROM polaris_service;

GRANT ALL ON ingestion_runs TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('023_ingestion_runs_grants.sql')
ON CONFLICT (filename) DO NOTHING;
