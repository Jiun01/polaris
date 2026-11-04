-- Migration 053 — tenant_promotions table + tenant_branches write policies
-- (Phase 8 / PR-9). Ref: phase-checklist/phase-08-portal.md PR-9 + ADR-042
-- (Promotions CRUD, universal across tiers) + ADR-041 Amendment (Branches
-- CRUD, Pro+ tier-gated UI-side; backend RLS is uniform across tiers
-- because tier-gating at the API layer would leak plan-tier into RLS
-- predicates).
--
-- Two parallel concerns shipped together because they're both portal-
-- driven CRUD surfaces gated by the same `app.current_tenant_id` GUC
-- pattern as every other tenant-scoped table:
--
--   (1) tenant_promotions (NEW)
--       Universal across tiers per ADR-042. Optional FK to tenant_branches
--       so a tenant can run a branch-specific promo (e.g. "20% off at KL
--       Office only"). NULL tenant_branch_id = applies tenant-wide.
--
--   (2) tenant_branches write policies (NEW POLICIES)
--       Migration 017a created the table + a SELECT-only policy with the
--       comment "Tenant-facing branch CRUD lands in the Pro+ multi-branch
--       phase." This is that phase. INSERT/UPDATE/DELETE policies added
--       here. Tier-gating ("Pro+ only see branches") happens UI-side via
--       <TierGate>; the backend RLS is uniform so a tenant who somehow
--       bypasses the UI gate still hits app.current_tenant_id scoping.
--
-- Rollback (local only):
--   DROP TABLE IF EXISTS tenant_promotions;
--   DROP POLICY IF EXISTS tenant_branches_self_insert ON tenant_branches;
--   DROP POLICY IF EXISTS tenant_branches_self_update ON tenant_branches;
--   DROP POLICY IF EXISTS tenant_branches_self_delete ON tenant_branches;

-- ---------------------------------------------------------------
-- (1) tenant_promotions
-- ---------------------------------------------------------------

CREATE TABLE tenant_promotions (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  tenant_branch_id  UUID NULL REFERENCES tenant_branches(id) ON DELETE SET NULL,
  title             TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  description       TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 2000),
  discount_text     TEXT NOT NULL CHECK (length(discount_text) BETWEEN 1 AND 100),
  terms             TEXT NULL CHECK (terms IS NULL OR length(terms) <= 4000),
  valid_from        TIMESTAMPTZ NOT NULL,
  valid_until       TIMESTAMPTZ NOT NULL,
  is_active         BOOLEAN NOT NULL DEFAULT TRUE,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT tenant_promotions_valid_range CHECK (valid_until > valid_from)
);

-- Hot read: list-by-tenant ordered by recency for the portal Promotions
-- list. Partial-active variant covers the bot-side lookup which filters
-- on `is_active AND now() BETWEEN valid_from AND valid_until`.
CREATE INDEX tenant_promotions_tenant_idx
  ON tenant_promotions (tenant_id, created_at DESC);

CREATE INDEX tenant_promotions_tenant_active_idx
  ON tenant_promotions (tenant_id, valid_from, valid_until)
  WHERE is_active;

ALTER TABLE tenant_promotions ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_promotions FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_promotions_self_all ON tenant_promotions
  FOR ALL
  USING       (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK  (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- Grants. The portal hits this via the polaris_anon role (PR-2 onwards);
-- service-role retains full access for any future admin maintenance path.
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_promotions TO polaris_anon;
GRANT ALL                            ON tenant_promotions TO polaris_service;

-- ---------------------------------------------------------------
-- (2) tenant_branches write policies (CRUD unlock)
-- ---------------------------------------------------------------

-- Migration 017a created the table with only a SELECT policy. Three new
-- policies below extend the surface to full CRUD scoped by the same GUC.
-- Tier-gating ("Pro+ only") is enforced UI-side via <TierGate>; backend
-- RLS stays uniform because per-tier predicates would couple the
-- security model to billing state.

CREATE POLICY tenant_branches_self_insert ON tenant_branches
  FOR INSERT
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_branches_self_update ON tenant_branches
  FOR UPDATE
  USING       (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK  (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_branches_self_delete ON tenant_branches
  FOR DELETE
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- Add write grants on tenant_branches — 017b granted SELECT only because
-- writes weren't unlocked yet. Append INSERT/UPDATE/DELETE so polaris_anon
-- can drive the CRUD endpoints landed in tenant_branches.py.
GRANT INSERT, UPDATE, DELETE ON tenant_branches TO polaris_anon;

INSERT INTO schema_migrations (filename) VALUES ('053_phase8_tenant_promotions_and_branch_writes.sql')
ON CONFLICT (filename) DO NOTHING;
