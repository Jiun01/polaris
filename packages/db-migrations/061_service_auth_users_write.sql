-- Migration 061 — Grant INSERT + DELETE on auth.users to polaris_service.
--
-- Context:
--   admin_tenants.py POST / does "INSERT INTO auth.users (email) VALUES ($1)"
--   when provisioning a new tenant admin.  Migration 010 only granted SELECT;
--   the INSERT was silently failing in all post-010 E2E runs (the wizard
--   endpoint returned 500 on any tenant create attempt against real Supabase).
--
--   The E2E internal seed endpoint (migration 061, internal_e2e.py) also needs
--   to create and hard-delete synthetic auth.users rows for test data isolation.
--
-- Security note:
--   polaris_service is a BYPASSRLS role used exclusively by the FastAPI
--   backend.  Granting INSERT+DELETE on auth.users is intentional: the admin
--   tenant-creation wizard is the SOLE provisioning path (ADR-012).  The grant
--   is narrow (INSERT, DELETE only; no UPDATE so passwords/MFA data are never
--   touched by the service role).
--
-- In the apply.py test harness (PostgreSQL service container) auth.users is
-- the shim from migration 001a — the GRANT runs against it without issue.
-- In Supabase Cloud the postgres superuser (used by apply.py via SUPABASE_DB_URL)
-- owns the auth schema and can grant on it.

GRANT INSERT, DELETE ON auth.users TO polaris_service;

INSERT INTO schema_migrations (filename)
VALUES ('061_service_auth_users_write.sql')
ON CONFLICT (filename) DO NOTHING;
