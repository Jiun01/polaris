-- Migration 098 — knowledge_gap_misses table + RLS + grants.
-- Knowledge Gap Detection / M1 (miss capture). Ref: .claude/prds/
-- knowledge-gap-detection.prd.md + .claude/plans/knowledge-gap-detection.plan.md.
--
-- One row per inbound where the bot FAILED to confidently answer. M1 captures
-- three signal types from cheap, already-emitted pipeline signals:
--   * 'no_retrieval'  — RAG returned zero chunks (nothing in the KB matched).
--   * 'low_retrieval' — top chunk rrf_score < the configured threshold
--                       (knowledge_gap_retrieval_threshold; 0.0 = disabled at
--                       GA until the score is tuned on real data — PRD open Q).
--   * 'handoff'       — the intent classifier routed a real question to a human.
-- NOT a miss: media-only / empty-body handoff (unsupported media, not a missing
-- answer) — excluded at the capture site so gaps don't fill with media noise.
--
-- Lifecycle:
--   * Written in the inbound pipeline (anon pool, RLS, tenant GUC set) — INSERT
--     only, fail-soft (a capture failure never blocks the customer reply).
--   * Read per-tenant by the M3 portal dashboard (anon, RLS) and scanned
--     cross-tenant by the M2 nightly clustering job (service pool, BYPASSRLS).
-- Append-only from the tenant surface: anon gets SELECT+INSERT (no UPDATE/
-- DELETE); the M2/M4 platform jobs run as polaris_service (ALL).
--
-- customer_question = the inbound text that went unanswered (a voice-note
-- transcript substitutes in upstream, so spoken questions are captured too).
-- message_id → the user message (the question); SET NULL on message delete so a
-- retention sweep of messages doesn't cascade-delete gap history.
--
-- Mirrors 091_followup_jobs (table + RLS + grants in one file). Indexes drive
-- (a) the per-tenant dashboard tail + M2 scan window and (b) signal-type
-- filtering for ranking.
--
-- Rollback (local dev only):
--   DROP TABLE IF EXISTS knowledge_gap_misses CASCADE;

CREATE TABLE knowledge_gap_misses (
  id                  UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           UUID             NOT NULL REFERENCES tenants(id)       ON DELETE CASCADE,
  conversation_id     UUID             NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  message_id          UUID             NULL     REFERENCES messages(id)      ON DELETE SET NULL,
  customer_question   TEXT             NOT NULL,
  signal_type         TEXT             NOT NULL CHECK (
                        signal_type IN ('no_retrieval', 'low_retrieval', 'handoff')
                      ),
  retrieval_top_score DOUBLE PRECISION NULL,
  channel             TEXT             NOT NULL,
  detected_language   TEXT             NULL,
  created_at          TIMESTAMPTZ      NOT NULL DEFAULT now()
);

-- Per-tenant tail: dashboard recent view + the M2 nightly scan window.
CREATE INDEX knowledge_gap_misses_tenant_created_idx
  ON knowledge_gap_misses (tenant_id, created_at DESC);

-- Signal-type filtering for dashboard ranking / per-signal breakdown.
CREATE INDEX knowledge_gap_misses_tenant_signal_created_idx
  ON knowledge_gap_misses (tenant_id, signal_type, created_at DESC);

ALTER TABLE knowledge_gap_misses ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_gap_misses FORCE  ROW LEVEL SECURITY;

-- RLS mirrors 028/091: USING + WITH CHECK gate on the app.current_tenant_id
-- GUC the inbound pipeline / portal request sets.
CREATE POLICY knowledge_gap_misses_tenant_isolation ON knowledge_gap_misses
  USING       (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK  (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- Anon (request/inbound pool) captures + reads its own misses: SELECT+INSERT
-- only (append-only — no corrections from the tenant surface). Service (M2
-- clustering + M4 resolve sweep) gets ALL for cross-tenant scan + cleanup.
GRANT SELECT, INSERT ON knowledge_gap_misses TO polaris_anon;
GRANT ALL           ON knowledge_gap_misses TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('098_knowledge_gap_misses.sql')
ON CONFLICT (filename) DO NOTHING;
