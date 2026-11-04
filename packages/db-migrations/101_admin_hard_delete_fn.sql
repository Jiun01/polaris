-- Migration 101 — admin hard-delete via SECURITY DEFINER function.
--
-- Supersedes migration 100's direct DELETE grants. Migration 100 granted
-- DELETE to polaris_service on five tables so the admin purge endpoint could
-- run raw DELETEs. For audit_logs that was WRONG: migration 012 deliberately
-- REVOKEs UPDATE/DELETE on audit_logs from every role to enforce the
-- append-only hash-chain audit trail (ADR-030). Handing polaris_service a
-- standing DELETE right defeats that ceiling for ALL code paths, not just the
-- sanctioned purge.
--
-- Correct pattern (same one migration 012 uses for audit_log_prev_hash): a
-- SECURITY DEFINER function owned by the migration role. The function runs
-- with the OWNER's privileges, so it can delete from append-only / un-granted
-- tables, while polaris_service keeps NO direct DELETE right. The only way to
-- trigger a purge is to call this one function, which the admin endpoint gates
-- behind require_platform_admin + a status='deleted' precondition.
--
-- Deletion order is FK-dependency safe:
--   1. audit_logs           — append-only, no CASCADE
--   2. broadcast_recipients — FK → audience_members(id), no CASCADE
--   3. audience_members     — FK → tenants(id), no CASCADE
--   4. demo_sessions        — FK → tenants(id), no CASCADE
--   5. tenant_invoices      — FK → tenants(id), no CASCADE
--   6. tenants              — CASCADE handles all remaining FK children
--
-- Tables referencing tenants via ON DELETE SET NULL (ingestion_runs,
-- sales_quotes.converted_to_tenant_id) need no handling: the RI trigger runs
-- as the table owner, not the caller, so no extra grant is required.
--
-- Rollback (local only):
--   DROP FUNCTION IF EXISTS admin_hard_delete_tenant(UUID);

-- Undo migration 100's direct grants — restore the append-only ceiling and
-- the single-sanctioned-path posture. REVOKE of a privilege that was never
-- granted is a harmless no-op, so this is safe whether or not 100 was applied.
REVOKE DELETE ON audit_logs           FROM polaris_service;
REVOKE DELETE ON audience_members     FROM polaris_service;
REVOKE DELETE ON broadcast_recipients FROM polaris_service;
REVOKE DELETE ON demo_sessions        FROM polaris_service;
REVOKE DELETE ON tenant_invoices      FROM polaris_service;

CREATE OR REPLACE FUNCTION admin_hard_delete_tenant(p_tenant_id UUID)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  DELETE FROM audit_logs           WHERE tenant_id = p_tenant_id;
  DELETE FROM broadcast_recipients WHERE tenant_id = p_tenant_id;
  DELETE FROM audience_members     WHERE tenant_id = p_tenant_id;
  DELETE FROM demo_sessions        WHERE tenant_id = p_tenant_id;
  DELETE FROM tenant_invoices      WHERE tenant_id = p_tenant_id;
  DELETE FROM tenants              WHERE id = p_tenant_id;
END;
$$;

-- Lock down EXECUTE: revoke from PUBLIC, grant only to the admin service role.
REVOKE ALL ON FUNCTION admin_hard_delete_tenant(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_hard_delete_tenant(UUID) TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('101_admin_hard_delete_fn.sql')
ON CONFLICT (filename) DO NOTHING;
