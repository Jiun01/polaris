-- Migration 088 — SECURITY DEFINER helper: tenant's primary (admin) user.
--
-- The admin Tenant-detail page needs the tenant's primary user (id + email)
-- so the "Impersonate user" action can deep-link into /impersonation with a
-- concrete target user. polaris_service cannot query auth.users directly
-- (Supabase forbids GRANT USAGE ON SCHEMA auth to custom roles — see
-- migration 066), so we expose the lookup through a SECURITY DEFINER function
-- owned by a superuser, mirroring public.get_portal_user_by_email.
--
-- Function: public.get_tenant_primary_user(p_tenant_id UUID)
--   Returns (user_id UUID, email TEXT) for the tenant's earliest 'admin'
--   member. At GA the only tenant_user_role is 'admin' (ADR-030), so this is
--   simply the first-provisioned member. Returns zero rows for a tenant with
--   no members.
--
-- Caller: apps/api/polaris_api/routers/admin_tenants.py get_tenant()
--
-- Rollback:
--   DROP FUNCTION IF EXISTS public.get_tenant_primary_user(UUID);

CREATE OR REPLACE FUNCTION public.get_tenant_primary_user(p_tenant_id UUID)
RETURNS TABLE(user_id UUID, email TEXT)
LANGUAGE sql
SECURITY DEFINER
-- Pin search_path so a malicious SET search_path cannot redirect the
-- function to a shadow schema (SECURITY DEFINER best practice).
SET search_path = public, auth
AS $$
  SELECT au.id, au.email
    FROM tenant_users tu
    JOIN auth.users   au ON au.id = tu.user_id
   WHERE tu.tenant_id = p_tenant_id
   ORDER BY tu.created_at ASC
   LIMIT 1;
$$;

-- Grant execute to polaris_service (the admin service-pool role) only.
GRANT EXECUTE ON FUNCTION public.get_tenant_primary_user(UUID)
  TO polaris_service;

-- Revoke from PUBLIC so arbitrary roles can't enumerate tenant members.
REVOKE EXECUTE ON FUNCTION public.get_tenant_primary_user(UUID) FROM PUBLIC;

INSERT INTO schema_migrations (filename) VALUES ('088_tenant_primary_user_lookup.sql')
ON CONFLICT (filename) DO NOTHING;
