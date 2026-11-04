-- Migration 019 — ingestion_requests grants (Phase 3 / PR-1).
-- Pairs with 018_ingestion_requests.sql.
--
-- polaris_anon: tenant intake endpoint inserts; tenant list/show endpoints
--   select; tenant cancel endpoint updates (RLS policies in 018 gate which
--   rows are touchable).
-- polaris_service: the desktop (Polaris Ingestion System) drains the queue,
--   updates status + counts, and writes chunks. Service-role bypasses RLS so
--   the desktop sees all tenants' pending rows in one pop_pending() pass.
--
-- Rollback (local only):
--   REVOKE ALL ON ingestion_requests FROM polaris_anon, polaris_service;

GRANT SELECT, INSERT, UPDATE ON ingestion_requests TO polaris_anon;
GRANT ALL                    ON ingestion_requests TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('019_ingestion_requests_grants.sql')
ON CONFLICT (filename) DO NOTHING;
