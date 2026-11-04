-- Migration 067 — SECURITY DEFINER helper: look up portal user email by UUID.
--
-- NextAuth v4's email callback calls adapter.updateUser({ id, emailVerified })
-- WITHOUT the user's email. Our adapter must return the full AdapterUser
-- (including email) so NextAuth can build a valid JWT. Without the correct
-- email the JWT is minted with email="" which breaks the session.
--
-- Function: public.get_portal_user_email_by_id(p_user_id UUID)
--   Returns the email TEXT for the auth.users row whose id matches, joined
--   against tenant_users to confirm an active membership exists.
--   Returns NULL if no matching provisioned user is found.
--
-- Caller: apps/portal/lib/auth/adapter.ts  updateUser()

CREATE OR REPLACE FUNCTION public.get_portal_user_email_by_id(p_user_id UUID)
RETURNS TEXT
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, auth
AS $$
  SELECT au.email
    FROM auth.users      au
    JOIN tenant_users    tu ON tu.user_id = au.id
   WHERE au.id = p_user_id
   LIMIT 1;
$$;

GRANT EXECUTE ON FUNCTION public.get_portal_user_email_by_id(UUID)
  TO polaris_service, polaris_anon;

REVOKE EXECUTE ON FUNCTION public.get_portal_user_email_by_id(UUID) FROM PUBLIC;
