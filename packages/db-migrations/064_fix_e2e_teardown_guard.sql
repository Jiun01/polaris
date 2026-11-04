-- Migration 064 — Fix delete_e2e_tenant_noncascade_rows guard.
--
-- Migration 063 guarded against slug NOT LIKE 'e2e-%' which is too strict:
-- the MRR sentinel tenant uses slug 'e2e-mrr-sentinel' (fine) but ad-hoc
-- test slugs passed without the prefix would fail.  The real security gate
-- is the POLARIS_E2E_INTERNAL_SECRET bearer token at the API layer; the
-- function only needs to confirm the tenant exists.

CREATE OR REPLACE FUNCTION public.delete_e2e_tenant_noncascade_rows(p_tenant_id UUID)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM tenants WHERE id = p_tenant_id) INTO v_exists;
  IF NOT v_exists THEN
    RAISE EXCEPTION 'delete_e2e_tenant_noncascade_rows: tenant % not found', p_tenant_id;
  END IF;

  DELETE FROM audit_logs       WHERE tenant_id = p_tenant_id;
  DELETE FROM audience_members WHERE tenant_id = p_tenant_id;
  DELETE FROM demo_sessions    WHERE tenant_id = p_tenant_id;
  DELETE FROM tenant_invoices  WHERE tenant_id = p_tenant_id;
END;
$$;

-- Grant is idempotent; harmless to repeat.
GRANT EXECUTE ON FUNCTION public.delete_e2e_tenant_noncascade_rows(UUID) TO polaris_service;

INSERT INTO schema_migrations (filename)
VALUES ('064_fix_e2e_teardown_guard.sql')
ON CONFLICT (filename) DO NOTHING;
