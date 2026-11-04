-- Migration 045 — Phase 6 grants + RLS policies.
-- Phase 6 / PR-1. Ref: docs/Polaris V2/04g_PHASE_6_DEEP.md §3.5.
-- Pairs with 041 / 042 / 043.
--
-- Grants + policies for the three new Phase 6 tables live here so a single
-- review surface covers RLS posture across the outbound introduction. The
-- per-table grants file pattern (the 026/027 split) would spread three
-- near-identical policies across three files; one consolidated file is the
-- cheaper review — same precedent as 036 in Phase 5.
--
-- Posture by table:
--   * tenant_templates       — tenant CRUD via polaris_anon under RLS.
--     The portal CRUD path mutates rows; the webhook lifecycle handler
--     UPDATEs status under polaris_service for cross-tenant scoping.
--   * broadcasts             — tenant CRUD via polaris_anon under RLS.
--     UPDATE is granted because tenant_admin operations (pause/resume/
--     cancel) flip status. Counter columns are also written via
--     polaris_service from the worker — granted via the polaris_service
--     blanket grant.
--   * broadcast_recipients   — tenant SELECT + UPDATE only.
--     INSERT goes through polaris_service from the broadcast worker
--     for bulk COPY (~10x throughput). UPDATE granted to polaris_anon so
--     admin tooling + portal recipient detail views can record status
--     transitions if required; primary writer is the worker.
--
-- All RLS policies follow the same pattern (USING + WITH CHECK on
-- app.current_tenant_id GUC) so a single grep over the schema confirms
-- uniform isolation. polaris_service has ALL on every table for the
-- worker + cron paths.

-- ---------------------------------------------------------------------
-- tenant_templates  (full tenant CRUD)
-- ---------------------------------------------------------------------
CREATE POLICY tenant_templates_tenant_isolation ON tenant_templates
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_templates TO polaris_anon;
GRANT ALL ON tenant_templates TO polaris_service;

-- ---------------------------------------------------------------------
-- broadcasts  (full tenant CRUD; worker writes counters via service pool)
-- ---------------------------------------------------------------------
CREATE POLICY broadcasts_tenant_isolation ON broadcasts
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON broadcasts TO polaris_anon;
GRANT ALL ON broadcasts TO polaris_service;

-- ---------------------------------------------------------------------
-- broadcast_recipients  (SELECT + UPDATE for polaris_anon;
--                        INSERT via polaris_service for bulk COPY)
-- ---------------------------------------------------------------------
CREATE POLICY broadcast_recipients_tenant_isolation ON broadcast_recipients
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

GRANT SELECT, UPDATE ON broadcast_recipients TO polaris_anon;
GRANT ALL ON broadcast_recipients TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('045_phase6_grants.sql')
ON CONFLICT (filename) DO NOTHING;
