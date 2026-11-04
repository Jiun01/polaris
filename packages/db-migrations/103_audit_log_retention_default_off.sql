-- 103_audit_log_retention_default_off.sql
--
-- Neutralise the audit_log_retention_days quota column.
--
-- Migration 058 added audit_log_retention_days with DEFAULT 365 and a COMMENT
-- describing an age-based "hard-delete audit_logs older than N days" sweep.
-- That sweep can never run: audit_logs is an append-only, hash-chained ledger
-- (ADR-030; migration 012 REVOKEs DELETE from polaris_service), and deleting
-- rows would break the chain verifier (services/audit.py::verify_chain). The
-- retention sweep no longer attempts it (see scheduled/retention_sweep.py).
--
-- This migration makes the schema reflect reality: the column is retained for
-- forward-compat but is not enforced. New rows default to 0 (= never) and
-- existing non-zero rows are zeroed so the column no longer advertises a
-- retention window that is never honoured. On-request erasure is handled by
-- admin_hard_delete_tenant (SECURITY DEFINER), not by this column.
--
-- Append-only + idempotent. No data loss (only flips an unused config value).
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   ALTER TABLE tenant_quotas ALTER COLUMN audit_log_retention_days SET DEFAULT 365;

ALTER TABLE tenant_quotas
  ALTER COLUMN audit_log_retention_days SET DEFAULT 0;

UPDATE tenant_quotas
   SET audit_log_retention_days = 0
 WHERE audit_log_retention_days <> 0;

COMMENT ON COLUMN tenant_quotas.audit_log_retention_days IS
  'DEPRECATED / not enforced. audit_logs is append-only (ADR-030); age-based '
  'purge is intentionally never performed (it would break the hash-chain '
  'verifier). Retained as a column for forward-compat; 0 = never. On-request '
  'erasure is handled by admin_hard_delete_tenant.';

INSERT INTO schema_migrations (filename)
VALUES ('103_audit_log_retention_default_off.sql')
ON CONFLICT (filename) DO NOTHING;
