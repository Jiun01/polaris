-- Migration 093 — platform_mrr_snapshots: persist the MRR rollup trend +
-- Sentry one-shot state that previously lived in an in-memory ring buffer.
--
-- Why: services/mrr_rollup.py kept the 30-day trend in a module-level
-- `deque(maxlen=30)` and the last-emitted Sentry band in a module global.
-- Both were lost on every API restart and diverged across multiple API
-- processes (each process had its own deque). This append-only table makes
-- "current MRR" = the latest row and "30-day trend" = the last 30 rows, so
-- both survive restarts and are shared across processes.
--
-- Ref: ADR-037 (SST tripwire bands) + its 2026-05-15 amendment, which named a
-- 1-row `platform_metrics` table for `current_mrr_myr`. We use an append-only
-- snapshot table instead so the 30-day trend the admin /financials UI already
-- renders is itself persisted (a single-row table cannot hold the trend). The
-- Sentry one-shot dedup now compares against the *previous persisted snapshot's*
-- band rather than a process-local variable. See ADR-062 in 09_DECISION_LOG.md.
--
-- Platform-scoped: service_pool only (BYPASSRLS). No RLS, no polaris_anon grant
-- — same posture as platform_health (migrations 005b / 011). Tenant code never
-- reads platform metrics.
--
-- Rollback (local only):
--   DROP TABLE IF EXISTS platform_mrr_snapshots;

CREATE TABLE platform_mrr_snapshots (
  id           BIGSERIAL PRIMARY KEY,
  mrr_myr      NUMERIC(12, 2) NOT NULL,
  alert_band   TEXT NOT NULL CHECK (alert_band IN ('green', 'amber', 'red')),
  computed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trend + "latest snapshot" reads are ORDER BY computed_at DESC, id DESC LIMIT N.
CREATE INDEX platform_mrr_snapshots_computed_at_idx
  ON platform_mrr_snapshots (computed_at DESC, id DESC);

GRANT ALL ON platform_mrr_snapshots TO polaris_service;
GRANT USAGE, SELECT ON SEQUENCE platform_mrr_snapshots_id_seq TO polaris_service;
-- polaris_anon: no grant.

INSERT INTO schema_migrations (filename) VALUES ('093_platform_mrr_snapshots.sql')
ON CONFLICT (filename) DO NOTHING;
