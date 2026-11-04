-- Migration 036 — flow_runtime grants + RLS policies (Phase 5 / PR-1).
-- Ref: docs/Polaris V2/04f_PHASE_5_DEEP.md §3.5.
-- Pairs with 032 / 033 / 034 / 035.
--
-- Grants + policies for the four flow-runtime tables live here so a
-- single review surface covers RLS posture across the runtime
-- introduction. Per-table grants files (the 026/027 pattern) would
-- spread four near-identical policies across four files; one
-- consolidated file is the cheaper review.
--
-- Posture by table:
--   * flow_definitions — tenant CRUD via polaris_anon under RLS. The
--     portal canvas mutates the draft in place; rollback can re-flip
--     ``current_version_id``; soft-delete sets ``archived_at``. UPDATE
--     and DELETE are both granted because both are valid tenant ops.
--   * flow_versions — INSERT + SELECT only (immutability). The lack of
--     UPDATE/DELETE grants is the immutability mechanism — RLS gates
--     visibility, missing grants block mutation.
--   * flow_runs — SELECT + INSERT + UPDATE. UPDATE so the runner can
--     flip ``running → completed/errored`` on the same row it
--     inserted at start. No DELETE: the Phase 9 retention sweeper runs
--     as polaris_service.
--   * flow_templates — platform-scoped; tenants get SELECT only.
--     polaris_service owns the seed + future refresh writes.
--
-- All three RLS policies follow the same pattern (USING + WITH CHECK
-- on ``app.current_tenant_id`` GUC) so a single grep over the schema
-- confirms uniform isolation.

-- ---------------------------------------------------------------------
-- flow_definitions
-- ---------------------------------------------------------------------
CREATE POLICY flow_definitions_tenant_isolation ON flow_definitions
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON flow_definitions TO polaris_anon;
GRANT ALL ON flow_definitions TO polaris_service;

-- ---------------------------------------------------------------------
-- flow_versions  (append-only — no UPDATE/DELETE grant)
-- ---------------------------------------------------------------------
CREATE POLICY flow_versions_tenant_isolation ON flow_versions
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

GRANT SELECT, INSERT ON flow_versions TO polaris_anon;
GRANT ALL ON flow_versions TO polaris_service;   -- platform retention may purge stale versions

-- ---------------------------------------------------------------------
-- flow_runs  (SELECT + INSERT + UPDATE)
-- ---------------------------------------------------------------------
CREATE POLICY flow_runs_tenant_isolation ON flow_runs
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE ON flow_runs TO polaris_anon;
GRANT ALL ON flow_runs TO polaris_service;

-- ---------------------------------------------------------------------
-- flow_templates  (platform-scoped; tenant read-only)
-- ---------------------------------------------------------------------
GRANT SELECT ON flow_templates TO polaris_anon;
GRANT SELECT, INSERT, UPDATE ON flow_templates TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('036_flow_runtime_grants.sql')
ON CONFLICT (filename) DO NOTHING;
