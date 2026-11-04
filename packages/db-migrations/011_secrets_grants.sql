-- Migration 011 — GRANTs on tenant_secrets + platform_health to PR-2 roles.
-- Phase 1 / PR-3 follow-up to PR-2's migration 010.
--
-- Migrations 005 + 005b created the tables; this migration grants role-level
-- DML on them. Lives in a separate file (not inlined in 005/005b) because
-- the roles polaris_anon and polaris_service are created by migration 010,
-- which sorts AFTER 005/005b in the filename-ordered apply sequence. A grant
-- in 005 referencing polaris_anon would fail on a fresh DB.
--
-- Privilege posture:
--   tenant_secrets   — polaris_anon full DML (RLS gates rows); polaris_service ALL
--   platform_health  — polaris_service ALL; polaris_anon NO ACCESS (no GRANT)
--                      The sentinel + key-fingerprint check is platform-only;
--                      tenant-scoped paths must never need to read it.
--
-- Rollback (local only):
--   REVOKE ALL ON tenant_secrets FROM polaris_anon, polaris_service;
--   REVOKE ALL ON platform_health FROM polaris_service;

GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_secrets TO polaris_anon;
GRANT ALL                            ON tenant_secrets TO polaris_service;

GRANT ALL                            ON platform_health TO polaris_service;
-- polaris_anon: no grant. Tenant code never reads platform_health.

INSERT INTO schema_migrations (filename) VALUES ('011_secrets_grants.sql')
ON CONFLICT (filename) DO NOTHING;
