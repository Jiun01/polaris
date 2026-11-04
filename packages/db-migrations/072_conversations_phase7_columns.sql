-- Migration 072 — conversations Phase 7 missing columns.
-- Ref: docs/Polaris V2/04h_PHASE_7_DEEP.md §3.1 + services/chatwoot/handoff.py.
-- Anchors: ADR-019 (branch-level business-hours override), Q-PHASE7-04.
--
-- Two columns were referenced in handoff.py's _LOAD_CTX_SQL query but
-- never shipped in migration 026 (conversations creation):
--
--   (a) tenant_branch_id — FK to tenant_branches, nullable.
--       Allows per-branch business-hours override in initiate_handoff().
--       The LEFT JOIN in _LOAD_CTX_SQL already handles NULL (no branch
--       assigned) correctly. Existing rows stay NULL.
--
--   (b) handoff_at — TIMESTAMPTZ, nullable.
--       Set by initiate_handoff() on CONFIRMED handoff to record when
--       the hand-off was completed. Mirrors tenants.handoff_at which
--       tracks the last platform-wide handoff. Existing rows stay NULL.
--
-- Without (a), every handoff node execution raised UndefinedColumnError,
-- aborting the transaction, which then caused InFailedSQLTransactionError
-- when update_flow_run_terminal() ran on the aborted connection.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   ALTER TABLE conversations
--     DROP COLUMN IF EXISTS tenant_branch_id,
--     DROP COLUMN IF EXISTS handoff_at;

ALTER TABLE conversations
  ADD COLUMN tenant_branch_id  UUID        NULL REFERENCES tenant_branches(id) ON DELETE SET NULL,
  ADD COLUMN handoff_at        TIMESTAMPTZ NULL;

COMMENT ON COLUMN conversations.tenant_branch_id IS
  'Optional FK to tenant_branches. When set, handoff service uses the branch''s business_hours + handoff_message_* overrides instead of the tenant defaults (ADR-019).';
COMMENT ON COLUMN conversations.handoff_at IS
  'Set by services/chatwoot/handoff.py::initiate_handoff on CONFIRMED handoff. NULL = no handoff yet or handoff still pending.';

INSERT INTO schema_migrations (filename) VALUES ('072_conversations_phase7_columns.sql')
ON CONFLICT (filename) DO NOTHING;
