-- Migration 062 — SECURITY DEFINER helpers for auth.users writes.
--
-- In Supabase Cloud the auth schema is owned by supabase_auth_admin; custom
-- roles like polaris_service cannot be granted INSERT/DELETE on auth.users even
-- by the postgres superuser (Supabase silently drops the grant).
--
-- The workaround: SECURITY DEFINER functions owned by postgres that run with
-- the owner's privileges and can therefore mutate auth.users.  polaris_service
-- is granted EXECUTE on these functions.
--
-- Two callers:
--   1. admin_tenants.py  POST /api/admin/tenants  — creates the tenant admin user.
--   2. internal_e2e.py   POST /internal/e2e/tenants — creates synthetic test users.
--
-- auth.users.id has no DEFAULT in Supabase; callers must supply a UUID.
-- The DELETE function guards against deleting non-E2E users (email suffix check).

-- ── get-or-create ──────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.upsert_auth_user_by_email(p_email TEXT)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, auth
AS $$
DECLARE
  v_id UUID;
BEGIN
  SELECT id INTO v_id
    FROM auth.users
   WHERE lower(email) = lower(p_email)
   LIMIT 1;

  IF v_id IS NULL THEN
    v_id := gen_random_uuid();
    INSERT INTO auth.users (id, email) VALUES (v_id, lower(p_email));
  END IF;

  RETURN v_id;
END;
$$;

-- ── safe delete (E2E only) ──────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.delete_e2e_auth_user(p_user_id UUID)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, auth
AS $$
BEGIN
  -- Guard: only delete rows whose email ends with the E2E domain to prevent
  -- accidental deletion of real users even if called with a wrong UUID.
  DELETE FROM auth.users
   WHERE id = p_user_id
     AND lower(email) LIKE '%@polaris-e2e.invalid';
END;
$$;

-- ── grants ─────────────────────────────────────────────────────────────────

GRANT EXECUTE ON FUNCTION public.upsert_auth_user_by_email(TEXT) TO polaris_service;
GRANT EXECUTE ON FUNCTION public.delete_e2e_auth_user(UUID)       TO polaris_service;

INSERT INTO schema_migrations (filename)
VALUES ('062_e2e_auth_user_helpers.sql')
ON CONFLICT (filename) DO NOTHING;
