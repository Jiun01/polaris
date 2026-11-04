-- Migration 006 — audit_logs (per-tenant hash chain, append-only).
-- Phase 1 / PR-4. Ref: docs/Polaris V2/04b_PHASE_1_DEEP.md §3.4 + ADR-003.
-- Anchors: ADR-003 (per-tenant SHA-256 chain), ADR-030 (platform_admin
--          SELECT only — tenant role gets no read).
--
-- Schema:
--   - prev_hash NULL for the first row in a tenant's chain.
--   - row_hash = SHA256(canonical_json(prev_hash || tenant_id || actor ||
--     action || before || after || created_at)). Same canonicalisation at
--     write and verify; deviating breaks the chain silently.
--   - created_at is set explicitly by services/audit.py (NOT DEFAULT now())
--     because it participates in the hash — the server clock at INSERT
--     time can't be known pre-insert.
--   - actor NULL = system/cron action (no human user).
--
-- RLS posture:
--   - ENABLE + FORCE row level security.
--   - INSERT policy gates anon role inserts to their own tenant_id —
--     prevents tenant A from logging audit entries under tenant B.
--   - NO SELECT policy → polaris_anon sees zero rows. polaris_service
--     bypasses RLS (BYPASSRLS attribute) so it reads freely without
--     needing an explicit policy.
--
-- Append-only enforcement lives in 012_audit_grants.sql (REVOKE UPDATE,
-- DELETE from both roles). The REVOKE is the real gate — RLS policies
-- can't restrict a role that already has table-level UPDATE/DELETE
-- privileges, and BYPASSRLS would skirt any RLS UPDATE/DELETE policy
-- anyway.
--
-- Rollback (local only):
--   DROP TABLE IF EXISTS audit_logs;

CREATE TABLE audit_logs (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id),
  actor       UUID NULL,
  action      TEXT NOT NULL CHECK (length(action) BETWEEN 1 AND 200),
  before      JSONB NULL,
  after       JSONB NULL,
  prev_hash   BYTEA NULL,
  row_hash    BYTEA NOT NULL CHECK (length(row_hash) = 32),
  created_at  TIMESTAMPTZ NOT NULL
);

-- Composite index: chain lookup ("last row for this tenant") + verifier
-- walk ("all rows for tenant, oldest first") both hit this.
CREATE INDEX audit_logs_tenant_created_idx
  ON audit_logs (tenant_id, created_at);

ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs FORCE  ROW LEVEL SECURITY;

-- INSERT: anon role must write under its own tenant_id. service_pool
-- (BYPASSRLS) writes any tenant_id — used by platform-admin actions.
CREATE POLICY audit_logs_self_insert ON audit_logs
  FOR INSERT
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- No SELECT policy. polaris_anon → 0 rows. polaris_service → all rows
-- (via BYPASSRLS). No UPDATE/DELETE policy either — those are revoked at
-- the role level in 012_audit_grants.sql for defense-in-depth.

INSERT INTO schema_migrations (filename) VALUES ('006_audit_logs.sql')
ON CONFLICT (filename) DO NOTHING;
