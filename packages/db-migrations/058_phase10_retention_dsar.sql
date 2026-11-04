-- Migration 058 — Phase 10 PR-3: retention sweep columns + DSAR export table.
-- ADR-029 (tenant lifecycle / 30-day grace), ADR-027 (demo data in DSAR window).
--
-- (a) Retention windows on tenant_quotas — honour per-tenant data-residency
--     commitments.  The nightly retention sweeper reads these to decide how
--     far back to hard-delete.
--
--     conversation_retention_days already exists (migration 007) and covers
--     messages.  Two new columns cover audit logs and ingestion request records.
--
-- (b) dsar_export_requests — tracks DSAR export jobs so tenants can poll
--     their export status (POST /api/tenant/privacy/export returns a
--     request_id; GET .../status/{id} polls this table).
--
-- Rollback (local only):
--   ALTER TABLE tenant_quotas
--     DROP COLUMN IF EXISTS audit_log_retention_days,
--     DROP COLUMN IF EXISTS ingestion_requests_retention_days;
--   DROP TABLE IF EXISTS dsar_export_requests;

-- ── (a) Retention columns ────────────────────────────────────────────────────

ALTER TABLE tenant_quotas
  ADD COLUMN IF NOT EXISTS audit_log_retention_days
      INT NOT NULL DEFAULT 365,
  ADD COLUMN IF NOT EXISTS ingestion_requests_retention_days
      INT NOT NULL DEFAULT 90;

COMMENT ON COLUMN tenant_quotas.audit_log_retention_days IS
  'Hard-delete audit_logs rows older than this many days (default 365). '
  '0 = never delete.';

COMMENT ON COLUMN tenant_quotas.ingestion_requests_retention_days IS
  'Hard-delete ingestion_requests rows older than this many days (default 90). '
  '0 = never delete.';

-- ── (b) DSAR export requests ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dsar_export_requests (
  id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  requested_by  UUID        NOT NULL,   -- tenant_users.user_id at request time
  status        TEXT        NOT NULL DEFAULT 'pending'
                            CHECK (status IN (
                              'pending', 'processing', 'ready', 'expired', 'failed'
                            )),
  signed_url    TEXT,                   -- populated when status = 'ready'
  expires_at    TIMESTAMPTZ,            -- signed_url expiry (24h SLA)
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at  TIMESTAMPTZ            -- set when status transitions to ready/failed
);

CREATE INDEX IF NOT EXISTS dsar_export_requests_tenant_created_idx
  ON dsar_export_requests (tenant_id, created_at DESC);

ALTER TABLE dsar_export_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE dsar_export_requests FORCE  ROW LEVEL SECURITY;

-- Tenant reads only their own export requests.
CREATE POLICY dsar_export_tenant_iso ON dsar_export_requests
  FOR ALL TO polaris_anon
  USING (tenant_id = (current_setting('app.current_tenant_id', true))::uuid);

-- Service role (BYPASSRLS) used by sweeper + admin.
GRANT SELECT, INSERT, UPDATE ON dsar_export_requests TO polaris_anon;
GRANT SELECT, INSERT, UPDATE ON dsar_export_requests TO polaris_service;
