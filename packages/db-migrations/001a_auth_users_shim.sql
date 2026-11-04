-- Migration 001a — auth.users FK target shim (local/CI dev only; no-op against Supabase).
-- Phase 1 / PR-1 prerequisite for migration 003.
--
-- Supabase Cloud ships a rich `auth.users` table managed by Supabase Auth.
-- Local Postgres and CI service containers don't, so any migration that
-- declares `REFERENCES auth.users(id)` would fail on a fresh DB.
--
-- We gate the CREATEs behind explicit information_schema existence checks
-- rather than relying on `IF NOT EXISTS`, because Postgres evaluates the
-- privilege requirement on the target schema *before* the existence check.
-- On Supabase the `auth` schema is owned by `supabase_auth_admin` and our
-- migration role doesn't have CREATE on it — so `CREATE TABLE IF NOT EXISTS
-- auth.users` raises InsufficientPrivilegeError even though the table is
-- already there. Checking `information_schema` first makes the CREATE truly
-- unreachable on Supabase, restoring the no-op intent.
--
-- Naming: `001a_` so it sorts immediately after `001_extensions.sql` and
-- before `002_tenants.sql` / `003_tenant_users.sql`. Confusion with the
-- canonical 05's mention of `001a_platform_health.sql` is intentional —
-- both can coexist; this PR ships only the auth shim and Phase 1 PR-3
-- adds `005b_platform_health.sql` per docs/Polaris V2/04b_PHASE_1_DEEP.md.
--
-- Rollback (local only): the shim is too tightly coupled to Supabase Auth
-- semantics to drop reliably — leave the table in place on rollback.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.schemata WHERE schema_name = 'auth'
  ) THEN
    EXECUTE 'CREATE SCHEMA auth';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'auth' AND table_name = 'users'
  ) THEN
    EXECUTE 'CREATE TABLE auth.users ('
         || '  id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),'
         || '  email TEXT UNIQUE'
         || ')';
  END IF;
END
$$;

INSERT INTO schema_migrations (filename) VALUES ('001a_auth_users_shim.sql')
ON CONFLICT (filename) DO NOTHING;
