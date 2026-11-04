-- Migration 003 — tenant_users N:N join (auth.users ↔ tenants) + role enum + RLS.
-- Phase 1 / PR-1. Ref: docs/Polaris V2/04b_PHASE_1_DEEP.md §3.2 + 05_DATA_MODEL_AND_API.md §2.1.
-- Anchors: ADR-030 (admin-only role enum at GA; operator/developer deferred to v2.1).
--
-- The auth.users FK target is Supabase-managed in production. Local Postgres
-- without Supabase Auth must seed a minimal `auth.users` shim before this
-- migration runs (the test fixture in apps/api/tests/conftest.py does this
-- via CREATE SCHEMA/TABLE IF NOT EXISTS; production Supabase ships it natively).
--
-- Rollback:
--   DROP TABLE IF EXISTS tenant_users CASCADE;
--   DROP TYPE  IF EXISTS tenant_user_role;

CREATE TYPE tenant_user_role AS ENUM ('admin');
-- 'operator' / 'developer' deferred to v2.1 per ADR-030.
-- ALTER TYPE tenant_user_role ADD VALUE 'operator' lands then.

CREATE TABLE tenant_users (
  tenant_id  UUID NOT NULL REFERENCES tenants(id)    ON DELETE CASCADE,
  user_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  role       tenant_user_role NOT NULL DEFAULT 'admin',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, tenant_id)
);

CREATE INDEX tenant_users_tenant_id_idx ON tenant_users (tenant_id);

ALTER TABLE tenant_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_users FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_users_self_select ON tenant_users
  FOR SELECT
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_users_self_insert ON tenant_users
  FOR INSERT
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_users_self_delete ON tenant_users
  FOR DELETE
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- No UPDATE policy: at GA only the 'admin' role exists, so role mutation is
-- meaningless. Drop + re-insert if a team membership ever needs replacing.

INSERT INTO schema_migrations (filename) VALUES ('003_tenant_users.sql')
ON CONFLICT (filename) DO NOTHING;
