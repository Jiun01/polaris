-- Migration 017b — tenant_branches grants (Phase 3 / PR-1 followup).
-- Pairs with 017a_tenant_branches.sql.
--
-- polaris_anon: SELECT only (tenant reads its own branches via RLS).
--   No DML grant — branch CRUD lands in the Pro+ multi-branch phase
--   under a separate admin/portal surface.
-- polaris_service: ALL (admin provisioning + the ingestion path,
--   which uses the FK target read-only).
--
-- Rollback (local only):
--   REVOKE ALL ON tenant_branches FROM polaris_anon, polaris_service;

GRANT SELECT ON tenant_branches TO polaris_anon;
GRANT ALL    ON tenant_branches TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('017b_tenant_branches_grants.sql')
ON CONFLICT (filename) DO NOTHING;
