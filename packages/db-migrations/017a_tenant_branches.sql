-- Migration 017a — tenant_branches (Phase 3 / PR-1 followup, landed in PR-5 review fix).
-- Ref: docs/Polaris V2/09_DECISION_LOG.md ADR-041 (branch-scoped KB).
--
-- Why this lands here, not in a dedicated branch-feature phase:
-- ADR-041 specs ``knowledge_chunks.tenant_branch_id UUID NULL REFERENCES
-- tenant_branches(id)`` AND ``ingestion_requests.tenant_branch_id`` with the
-- same FK shape. Migrations 018 + 020 ship those FKs, so the referenced
-- table must exist before they apply. Migration 009 (audience_members)
-- deliberately left ``tenant_branch_id`` UNREFERENCED with a comment
-- saying "FK added when tenant_branches lands"; this is the migration
-- that fulfils that promise.
--
-- The full Pro+ multi-branch product surface (CRUD endpoints, portal
-- screens, per-branch routing) lives in a later phase. PR-5 ships ONLY
-- the table + the RLS scaffolding so the FK targets resolve and the
-- branch-scoped filter in match_knowledge_chunks_hybrid (migration 024)
-- has somewhere to point. Branch CRUD endpoints follow when the multi-
-- branch product feature is built out.
--
-- 1:N with tenants. (tenant_id, name) UNIQUE so a tenant can name their
-- own branches without collision but two tenants may reuse the same
-- label ("KL Office" appears in 50 tenants — fine, each is its own row).
--
-- Rollback (local only):
--   DROP TABLE IF EXISTS tenant_branches CASCADE;

CREATE TABLE tenant_branches (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name       TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT tenant_branches_tenant_name_unique UNIQUE (tenant_id, name)
);

CREATE INDEX tenant_branches_tenant_idx ON tenant_branches (tenant_id);

ALTER TABLE tenant_branches ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_branches FORCE  ROW LEVEL SECURITY;

-- Tenants see their own branches via the same GUC-keyed pattern as
-- every other tenant-scoped table.
CREATE POLICY tenant_branches_self_select ON tenant_branches
  FOR SELECT
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- INSERT / UPDATE / DELETE: no policy yet. Tenant-facing branch CRUD
-- lands in the Pro+ multi-branch phase. Until then, branch creation
-- happens via service_pool (admin) only — defence-in-depth so no
-- accidental branch row appears outside a deliberate provisioning flow.

INSERT INTO schema_migrations (filename) VALUES ('017a_tenant_branches.sql')
ON CONFLICT (filename) DO NOTHING;
