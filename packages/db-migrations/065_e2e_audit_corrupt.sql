-- Migration 065: E2E helper — corrupt audit_log row_hash (Phase 9 PR-12)
--
-- polaris_service has INSERT + SELECT on audit_logs but REVOKE UPDATE (ADR-030
-- append-only invariant). The E2E audit-chain-corrupt endpoint needs to flip one
-- row_hash to an all-0xFF sentinel without granting UPDATE to the service role.
--
-- Solution: SECURITY DEFINER function owned by the migration runner (postgres /
-- supabase_admin) that runs with the owner's privileges, same pattern as
-- migration 063 (delete_e2e_tenant_noncascade_rows).
--
-- GRANT EXECUTE is given ONLY to polaris_service — no other role can call this.
-- The function name, its e2e_ prefix, and the sentinel value make its purpose
-- unambiguous in the audit log.

CREATE OR REPLACE FUNCTION public.corrupt_e2e_audit_hash(p_tenant_id UUID)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_id UUID;
BEGIN
    -- 32 bytes of 0xFF: an unmistakable sentinel that is never a real SHA-256.
    UPDATE audit_logs
       SET row_hash = decode(repeat('ff', 32), 'hex')
     WHERE id = (
           SELECT id
             FROM audit_logs
            WHERE tenant_id = p_tenant_id
            ORDER BY created_at DESC
            LIMIT 1
     )
    RETURNING id INTO v_id;

    RETURN v_id;
END;
$$;

-- Only polaris_service may invoke this function.
GRANT EXECUTE ON FUNCTION public.corrupt_e2e_audit_hash(UUID) TO polaris_service;
