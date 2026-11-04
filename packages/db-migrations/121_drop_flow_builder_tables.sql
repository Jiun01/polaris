-- Migration 121 — drop orphaned flow-builder tables (flow_runtime retirement follow-up).
-- Ref: audit-batch-d-flow-runtime-retirement-report.md "Undone / out of scope"
-- (flow_runtime service code was fully retired 2026-07-02; this migration
-- retires the last remaining artifact — the DB schema).
--
-- Prod-verified (2026-07-02 audit): 0 published flows, 0 flow_runs ever
-- recorded. These four tables (032-035) plus their grants/RLS (036) and the
-- flow_run_kind / flow_run_status enums (034) are dead schema with zero
-- live code readers or writers left in apps/ or packages/ after Batch D.
--
-- Drop order (children first, satisfies every FK with NO CASCADE keyword
-- needed anywhere):
--   0. messages_flow_run_id_fk — the FK on messages(flow_run_id) referencing
--                          flow_runs(id) (added in 037) must be dropped FIRST:
--                          a bare DROP TABLE flow_runs would otherwise fail
--                          with "other objects depend on it" (the constraint
--                          lives on messages but depends on flow_runs).
--   1. flow_definitions_current_version_fk — flow_definitions and
--                          flow_versions form a CIRCULAR FK pair
--                          (flow_versions.flow_definition_id -> flow_definitions
--                          AND flow_definitions.current_version_id ->
--                          flow_versions, both from 033), so neither table can
--                          be dropped bare while both constraints exist. Drop
--                          this one explicitly to break the cycle.
--   2. flow_runs         — outbound FKs only (-> flow_versions, conversations,
--                          messages) once step 0 removes the inbound one.
--   3. flow_versions     — its flow_definition_id fkey drops with the table,
--                          unblocking step 4.
--   4. flow_definitions  — nothing references it once 1+3 are gone.
--   5. flow_templates    — independent (no FKs in or out); dropped last for
--                          readability only.
--
-- flow_run_kind / flow_run_status ENUMs (034) are dropped AFTER all four
-- tables since flow_runs.kind / flow_runs.terminal_status are the only
-- columns using them.
--
-- Policies (flow_definitions_tenant_isolation, flow_versions_tenant_isolation,
-- flow_runs_tenant_isolation from 036; flow_templates_public_read from 107)
-- and all GRANTs (036) are table-scoped metadata — they are removed
-- automatically by DROP TABLE, nothing to do explicitly. Same for the six
-- flow-table indexes (dropped automatically with their tables) and the 4
-- flow_templates seed rows from migration 038 (further UPDATEd by 079/082 —
-- those two migrations become no-op UPDATEs matching zero rows on any DB
-- that applies them before this one, which filename ordering guarantees).
--
-- CRITICAL EXCLUSION: tenant_templates (migration 041, broadcast message
-- templates) is a completely unrelated table — not touched, does not share
-- any FK/policy/grant with the four tables above.
--
-- messages.flow_run_id (added in 028) is left in place as an orphaned
-- nullable UUID column — cosmetic only, out of scope for this migration
-- (touching the messages table is a separate concern).
--
-- Rollback (local dev only — irreversible in prod without a schema
-- re-migration from 032-039/107; there is no data to restore, flow_templates'
-- 4 seed rows would need to be re-inserted from migration 038's INSERT):
--   Re-run migrations 032, 033, 034, 035, 036, 038 (in that order) to
--   recreate tables + grants/RLS + seed rows; 107's flow_templates RLS
--   policy would also need re-applying.

ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_flow_run_id_fk;
ALTER TABLE IF EXISTS flow_definitions
  DROP CONSTRAINT IF EXISTS flow_definitions_current_version_fk;

DROP TABLE IF EXISTS flow_runs;
DROP TABLE IF EXISTS flow_versions;
DROP TABLE IF EXISTS flow_definitions;
DROP TABLE IF EXISTS flow_templates;

DROP TYPE IF EXISTS flow_run_kind;
DROP TYPE IF EXISTS flow_run_status;

INSERT INTO schema_migrations (filename) VALUES ('121_drop_flow_builder_tables.sql')
ON CONFLICT (filename) DO NOTHING;
