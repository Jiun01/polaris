-- Migration 066 — SECURITY DEFINER helper for portal auth adapter.
--
-- The portal's NextAuth adapter needs to look up auth.users by email.
-- Supabase does not allow GRANT USAGE ON SCHEMA auth to custom roles, so
-- polaris_service cannot query auth.users directly (permission denied for
-- schema auth). The Supabase-recommended pattern is a SECURITY DEFINER
-- function owned by a superuser; the function runs as its owner (postgres)
-- which has full auth.* access.
--
-- Function: public.get_portal_user_by_email(email TEXT)
--   Returns (id UUID, email TEXT) for the auth.users row whose email
--   matches the given address (case-insensitive), joined against
--   tenant_users to confirm the email belongs to a provisioned tenant member.
--   Returns zero rows if no match.
--
-- Caller: apps/portal/lib/auth/adapter.ts  getUserByEmail()

CREATE OR REPLACE FUNCTION public.get_portal_user_by_email(p_email TEXT)
RETURNS TABLE(id UUID, email TEXT)
LANGUAGE sql
SECURITY DEFINER
-- Pin to the current search_path so a malicious SET search_path cannot
-- redirect the function to a shadow schema (SECURITY DEFINER best practice).
SET search_path = public, auth
AS $$
  SELECT au.id, au.email
    FROM auth.users      au
    JOIN tenant_users    tu ON tu.user_id = au.id
   WHERE lower(au.email) = lower(p_email)
   LIMIT 1;
$$;

-- Grant execute to polaris_service (used by the portal auth adapter)
-- and polaris_anon (symmetry; not currently called from anon paths
-- but harmless and keeps the grant surface consistent with 010_tenant_roles).
GRANT EXECUTE ON FUNCTION public.get_portal_user_by_email(TEXT)
  TO polaris_service, polaris_anon;

-- Revoke from PUBLIC so arbitrary roles can't call it.
REVOKE EXECUTE ON FUNCTION public.get_portal_user_by_email(TEXT) FROM PUBLIC;
