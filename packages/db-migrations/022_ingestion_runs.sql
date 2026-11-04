-- Migration 022 — ingestion_runs (Phase 3 / PR-1).
-- Ref: docs/Polaris V2/04d_PHASE_3_DEEP.md §3.5.
--
-- Platform-only heartbeat table. Each row = one desktop processing cycle
-- (either a single request or a full queue drain). Phase 9 admin reads this
-- as "worker last seen Xm ago" to monitor desktop health.
--
-- No RLS posture: every row is per-workstation, not per-tenant. tenant_id
-- is recorded for filtering ("show me runs touching tenant X") but is not
-- the access key. polaris_anon never reads this; only polaris_service (admin
-- routes + the desktop itself writing heartbeats).
--
-- The no-PII rule still applies: errors JSONB stores stack traces
-- + request_id refs, never raw chunk content or tenant secrets.
--
-- Rollback (local only):
--   DROP TABLE IF EXISTS ingestion_runs CASCADE;

CREATE TABLE ingestion_runs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  worker_id       TEXT NOT NULL,
  tenant_id       UUID NULL REFERENCES tenants(id) ON DELETE SET NULL,
  request_id      UUID NULL REFERENCES ingestion_requests(id) ON DELETE SET NULL,
  started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at     TIMESTAMPTZ NULL,
  ok              BOOLEAN NULL,
  rows_processed  INT NOT NULL DEFAULT 0,
  errors          JSONB NOT NULL DEFAULT '[]'::jsonb,
  notes           TEXT NULL
);

CREATE INDEX ingestion_runs_worker_started_idx
  ON ingestion_runs (worker_id, started_at DESC);

-- Intentionally NO RLS: platform-only. Access enforced via grants (023).

INSERT INTO schema_migrations (filename) VALUES ('022_ingestion_runs.sql')
ON CONFLICT (filename) DO NOTHING;
