-- Migration 005 — tenant_secrets table + RLS + per-table GRANTs.
-- Phase 1 / PR-3. Ref: docs/Polaris V2/04b_PHASE_1_DEEP.md §3.4 (PR-3 spec).
-- Anchors: ADR-002 (RLS-by-default), PRD §11 (per-tenant credentials).
--
-- Schema deviation from deep-dive spec: deep-dive lists `algo` and `version`
-- as separate columns. We store them inline because the secrets module
-- already version-tags its wire format (1-byte VERSION prefix per
-- services/secrets.py), so a separate `version INT` column would duplicate
-- the same byte. Algo is a code constant (xchacha20poly1305) for v2 GA;
-- the version byte is the upgrade lever per ADR-on-deck. Splitting
-- nonce + ciphertext-with-tag matches the deep-dive shape.
--
-- GRANTs are inline because roles polaris_anon / polaris_service exist
-- after migration 010 (lexically 005 < 010 BUT migrations apply in
-- filename-sorted order — wait, 005 sorts before 010, so 010 hasn't run
-- yet when 005 runs on a fresh DB). Therefore grants live in 010 for
-- the PR-1 tables, but for PR-3 tables (005, 005b) we add a follow-up
-- grants migration 011_secrets_grants.sql that runs after 010.
--
-- Rollback (local only):
--   DROP TABLE IF EXISTS tenant_secrets CASCADE;

CREATE TABLE tenant_secrets (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,
  ciphertext  BYTEA NOT NULL,
  nonce       BYTEA NOT NULL,
  algo        TEXT NOT NULL DEFAULT 'xchacha20poly1305',
  version     INT  NOT NULL DEFAULT 1,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  rotated_at  TIMESTAMPTZ,
  UNIQUE (tenant_id, kind)
);

CREATE INDEX tenant_secrets_tenant_id_idx ON tenant_secrets (tenant_id);

ALTER TABLE tenant_secrets ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_secrets FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_secrets_self_all ON tenant_secrets
  FOR ALL
  USING       (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK  (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

INSERT INTO schema_migrations (filename) VALUES ('005_tenant_secrets.sql')
ON CONFLICT (filename) DO NOTHING;
