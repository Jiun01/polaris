-- Migration 074 — tenant_branches handoff-aware columns.
-- Ref: docs/Polaris V2/04h_PHASE_7_DEEP.md §3.1 (branch-level business-hours override).
-- Anchors: ADR-019 (business-hours-aware handoff), services/chatwoot/handoff.py.
--
-- handoff.py's _LOAD_CTX_SQL LEFT JOINs tenant_branches and selects:
--   tb.business_hours, tb.handoff_message_in_hours, tb.handoff_message_after_hours
-- None of these columns existed in tenant_branches (017a + 071 only added
-- id/tenant_id/name/created_at/metadata/updated_at). Every handoff node
-- execution failed with UndefinedColumnError, aborting the asyncpg transaction.
--
-- Three nullable columns (no NOT NULL default — existing branches have no
-- configured hours/messages; NULL means "inherit from tenant defaults"):
--
--   business_hours             JSONB NULL — same shape as tenants.business_hours.
--                              {"0":{"open":"09:00","close":"18:00"},...,"6":null}
--                              NULL = use tenant default.
--
--   handoff_message_in_hours   TEXT NULL — customer-facing message during business
--                              hours. NULL = use tenant default.
--
--   handoff_message_after_hours TEXT NULL — customer-facing message outside business
--                              hours. NULL = use tenant default.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   ALTER TABLE tenant_branches
--     DROP COLUMN IF EXISTS business_hours,
--     DROP COLUMN IF EXISTS handoff_message_in_hours,
--     DROP COLUMN IF EXISTS handoff_message_after_hours;

ALTER TABLE tenant_branches
  ADD COLUMN business_hours              JSONB NULL,
  ADD COLUMN handoff_message_in_hours    TEXT  NULL,
  ADD COLUMN handoff_message_after_hours TEXT  NULL;

COMMENT ON COLUMN tenant_branches.business_hours IS
  'Per-branch business-hours schedule (ADR-019). Same shape as tenants.business_hours. NULL = inherit tenant default.';
COMMENT ON COLUMN tenant_branches.handoff_message_in_hours IS
  'Customer-facing handoff message during business hours. NULL = inherit tenant default.';
COMMENT ON COLUMN tenant_branches.handoff_message_after_hours IS
  'Customer-facing handoff message outside business hours. NULL = inherit tenant default.';

INSERT INTO schema_migrations (filename) VALUES ('074_tenant_branches_handoff_columns.sql')
ON CONFLICT (filename) DO NOTHING;
