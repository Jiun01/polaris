-- Migration 063 — SECURITY DEFINER helper for E2E tenant hard-delete.
--
-- Context:
--   internal_e2e.py DELETE /internal/e2e/tenants/{id} needs to hard-delete
--   synthetic tenant rows.  Several tables that reference tenants lack ON
--   DELETE CASCADE and polaris_service has had DELETE deliberately REVOKEd
--   (e.g. audit_logs — append-only per ADR-030, migration 012).
--
--   This function runs as postgres (SECURITY DEFINER) and may therefore
--   delete from those restricted tables.  It is gated to slugs that start
--   with 'e2e-' so it cannot be used to nuke production data even if called
--   with a wrong tenant_id.
--
-- Tables purged (non-cascade children of tenants):
--   audit_logs       — FK without CASCADE; seed inserts one row.
--   audience_members — FK without CASCADE; E2E tenants may accumulate these.
--   demo_sessions    — FK without CASCADE; typically empty for synthetic tenants.
--   tenant_invoices  — FK without CASCADE; typically empty for synthetic tenants.
--
-- After this function returns the caller may safely DELETE FROM tenants.

CREATE OR REPLACE FUNCTION public.delete_e2e_tenant_noncascade_rows(p_tenant_id UUID)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_slug TEXT;
BEGIN
  -- Guard: only operate on E2E tenants.
  SELECT slug INTO v_slug FROM tenants WHERE id = p_tenant_id LIMIT 1;
  IF v_slug IS NULL OR v_slug NOT LIKE 'e2e-%' THEN
    RAISE EXCEPTION 'delete_e2e_tenant_noncascade_rows: tenant % is not an E2E tenant (slug=%)', p_tenant_id, v_slug;
  END IF;

  DELETE FROM audit_logs       WHERE tenant_id = p_tenant_id;
  DELETE FROM audience_members WHERE tenant_id = p_tenant_id;
  DELETE FROM demo_sessions    WHERE tenant_id = p_tenant_id;
  DELETE FROM tenant_invoices  WHERE tenant_id = p_tenant_id;
END;
$$;

GRANT EXECUTE ON FUNCTION public.delete_e2e_tenant_noncascade_rows(UUID) TO polaris_service;

INSERT INTO schema_migrations (filename)
VALUES ('063_e2e_teardown_helper.sql')
ON CONFLICT (filename) DO NOTHING;
