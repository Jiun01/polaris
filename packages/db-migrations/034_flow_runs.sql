-- Migration 034 — flow_runs (Phase 5 / PR-1).
-- Ref: docs/Polaris V2/04f_PHASE_5_DEEP.md §3.3 + 06_FLOW_BUILDER_SPEC.md §7 + §12.
-- Anchors: ADR-007 (every run persists a row), ADR-050 (no in-app simulator UI but the
--          API exists; ``kind='simulation'`` is the discriminator), ADR-051 (terminal
--          status drives ``flow.run.completed`` / ``flow.run.errored`` events — at GA
--          the events are persisted, not dispatched; outbound webhook worker is v3.0).
--
-- One row per flow execution. The runner inserts at start (status=
-- 'running'), then UPDATEs on terminal with the final status, step
-- count, snapshot, and (for simulator runs) the full step_trace.
--
-- Two ENUMs:
--   * ``flow_run_kind``  — discriminates production traffic from
--     simulator dry-runs. Phase 9 retention sweeper uses this to apply
--     different TTLs (target: 90d for production, 7d for simulation —
--     see Q-PHASE5-02 in 04f §11).
--   * ``flow_run_status`` — running → terminal (completed / errored /
--     aborted). ``aborted`` is reserved for v2.1's cancel-in-flight
--     API; GA never writes it.
--
-- ``conversation_id`` and ``trigger_message_id`` are nullable because
-- simulator runs have no conversation context. The runner is permitted
-- to construct synthetic IncomingMessage shapes for the publish smoke
-- test, so a simulator row may also have NULL trigger_message_id.
--
-- ``step_trace`` is JSONB nullable on purpose. Production runs write
-- NULL by default — capturing every step's variables_after for every
-- message at scale would blow column storage (~32 KB × millions of
-- messages = unsustainable). The runner only fills step_trace when
-- ``kind='simulation'`` OR the request flagged ``?trace=true``. The
-- simulator portal panel (Phase 8) reads back from this column.
--
-- ``variables_snapshot`` is the final flat RunContext.variables dict
-- at terminal; it's bounded by FLOW_RUNTIME_VARS_MAX_KB (32 KB) per
-- runner invariant. Persisted unconditionally so the v2.1+ wait-node
-- resumption path has a restoration source (Phase 5 ships a snapshot
-- stub that raises NotImplementedError — the column exists ahead of
-- the feature).
--
-- ``error`` JSONB carries ``{code, message, node_id}`` on terminal=
-- 'errored'. Never NULL when terminal_status='errored'; always NULL
-- when 'completed'. Enforced by application code rather than a CHECK
-- so partial-rollout fixes don't trip the constraint on legacy rows.
--
-- Indexes:
--   * (tenant_id, started_at DESC) — Phase 8 portal "recent runs" feed
--     and Phase 9 retention sweep.
--   * partial on conversation_id — used by the "show flow history for
--     this conversation" tab; NULL rows (simulator) skipped.

CREATE TYPE flow_run_kind   AS ENUM ('production', 'simulation');
CREATE TYPE flow_run_status AS ENUM ('running', 'completed', 'errored', 'aborted');

CREATE TABLE flow_runs (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  flow_version_id      UUID NOT NULL REFERENCES flow_versions(id),
  conversation_id      UUID NULL REFERENCES conversations(id) ON DELETE SET NULL,
  trigger_message_id   UUID NULL REFERENCES messages(id) ON DELETE SET NULL,
  kind                 flow_run_kind   NOT NULL,
  terminal_status      flow_run_status NOT NULL DEFAULT 'running',
  step_count           INT             NOT NULL DEFAULT 0,
  started_at           TIMESTAMPTZ     NOT NULL DEFAULT now(),
  finished_at          TIMESTAMPTZ     NULL,
  variables_snapshot   JSONB           NOT NULL DEFAULT '{}'::jsonb,
  step_trace           JSONB           NULL,
  error                JSONB           NULL,
  created_at           TIMESTAMPTZ     NOT NULL DEFAULT now()
);

CREATE INDEX flow_runs_tenant_started_idx
  ON flow_runs (tenant_id, started_at DESC);

CREATE INDEX flow_runs_conversation_idx
  ON flow_runs (conversation_id, started_at DESC)
  WHERE conversation_id IS NOT NULL;

CREATE INDEX flow_runs_flow_version_idx
  ON flow_runs (flow_version_id, started_at DESC);

ALTER TABLE flow_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE flow_runs FORCE ROW LEVEL SECURITY;

INSERT INTO schema_migrations (filename) VALUES ('034_flow_runs.sql')
ON CONFLICT (filename) DO NOTHING;
