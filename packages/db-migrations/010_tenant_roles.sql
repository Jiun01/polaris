-- Migration 010 — Polaris Postgres roles + per-table GRANTs.
-- Phase 1 / PR-2. Ref: docs/Polaris V2/04b_PHASE_1_DEEP.md §3.4.
-- Anchors: ADR-002 (dual asyncpg pool — anon_pool with NOBYPASSRLS,
--          service_pool with BYPASSRLS reserved for cron + admin).
--
-- Operator step AFTER applying this migration:
--   ALTER ROLE polaris_anon    WITH PASSWORD '<generate-with-pwgen>';
--   ALTER ROLE polaris_service WITH PASSWORD '<generate-with-pwgen>';
-- Then put those values in POLARIS_DB_URL_ANON / POLARIS_DB_URL_SERVICE in .env.
-- See ../docs/internal-ops/RUNBOOK.md "PR-2 role bootstrap". Passwords NEVER enter git history.
--
-- The DO/EXCEPTION blocks make this idempotent: re-running the migration on a
-- DB where roles already exist is a no-op (CREATE ROLE doesn't have IF NOT
-- EXISTS as a standalone clause).
--
-- Rollback (local only):
--   REVOKE ALL ON tenants, tenant_users, tenant_channels FROM polaris_anon, polaris_service;
--   REVOKE USAGE ON SCHEMA public, auth FROM polaris_anon, polaris_service;
--   DROP ROLE IF EXISTS polaris_anon;
--   DROP ROLE IF EXISTS polaris_service;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'polaris_anon') THEN
    CREATE ROLE polaris_anon LOGIN NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'polaris_service') THEN
    CREATE ROLE polaris_service LOGIN BYPASSRLS;
  END IF;
END
$$;

-- Schema usage — both roles need to see the public + auth schemas at all.
GRANT USAGE ON SCHEMA public TO polaris_anon, polaris_service;
GRANT USAGE ON SCHEMA auth   TO polaris_anon, polaris_service;

-- Per-table DML for the Phase 1 PR-1 tables. polaris_anon's DML is gated by
-- RLS at row level; polaris_service bypasses RLS for cron + admin paths.
GRANT SELECT, INSERT, UPDATE, DELETE ON tenants         TO polaris_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_users    TO polaris_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_channels TO polaris_anon;

GRANT ALL ON tenants         TO polaris_service;
GRANT ALL ON tenant_users    TO polaris_service;
GRANT ALL ON tenant_channels TO polaris_service;

-- auth.users is read-only from Polaris's side (Supabase Auth owns mutations).
GRANT SELECT ON auth.users TO polaris_anon, polaris_service;

-- schema_migrations is platform-owned; only service_pool touches it.
GRANT SELECT ON schema_migrations TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('010_tenant_roles.sql')
ON CONFLICT (filename) DO NOTHING;
