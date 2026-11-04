-- Migration 012 — audit_logs GRANTs + append-only REVOKEs.
-- Phase 1 / PR-4 follow-up to PR-2's migration 010.
--
-- Append-only enforcement: REVOKE UPDATE, DELETE from both roles. This
-- is the real gate; RLS UPDATE/DELETE policies can't constrain a role
-- that has table-level UPDATE/DELETE privileges, and BYPASSRLS would
-- skirt RLS anyway. The REVOKE is the strict ceiling.
--
-- SELECT posture (per ADR-030):
--   polaris_anon    — NO SELECT grant. Tenant-side code never reads audit_logs.
--   polaris_service — SELECT (for verify_chain + platform-admin viewer).
--
-- INSERT posture:
--   Both roles — GRANT INSERT. anon writes are RLS-gated to own tenant
--   (policy in 006); service writes any tenant via BYPASSRLS.
--
-- Rollback (local only):
--   REVOKE ALL ON audit_logs FROM polaris_anon, polaris_service;

GRANT INSERT ON audit_logs TO polaris_anon;
GRANT INSERT ON audit_logs TO polaris_service;
GRANT SELECT ON audit_logs TO polaris_service;

-- Defensive REVOKE — explicit even though we never granted UPDATE/DELETE.
-- Future grants (e.g. someone adds GRANT ALL by mistake) would be neutered.
REVOKE UPDATE, DELETE ON audit_logs FROM polaris_anon;
REVOKE UPDATE, DELETE ON audit_logs FROM polaris_service;

-- SECURITY DEFINER helper: returns the chain head's row_hash for a tenant.
-- services/audit.py::record() calls this to fetch prev_hash without needing
-- a SELECT grant on audit_logs — the function runs as its owner (the
-- migration role) which can read the table, and exposes only the single
-- bytea result to the caller. Preserves ADR-030's "tenant role never reads
-- audit_logs" invariant.
CREATE OR REPLACE FUNCTION audit_log_prev_hash(p_tenant_id UUID)
RETURNS BYTEA
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_prev BYTEA;
BEGIN
  SELECT row_hash INTO v_prev
  FROM audit_logs
  WHERE tenant_id = p_tenant_id
  ORDER BY created_at DESC, id DESC
  LIMIT 1;
  RETURN v_prev;
END;
$$;

-- Lock down EXECUTE: revoke from PUBLIC first, then grant to the two
-- known roles. Without the PUBLIC revoke, every role on the DB could
-- call the function and read any tenant's chain head.
REVOKE ALL ON FUNCTION audit_log_prev_hash(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION audit_log_prev_hash(UUID) TO polaris_anon;
GRANT EXECUTE ON FUNCTION audit_log_prev_hash(UUID) TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('012_audit_grants.sql')
ON CONFLICT (filename) DO NOTHING;
