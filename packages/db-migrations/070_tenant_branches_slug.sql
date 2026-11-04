-- Migration 070: add slug to tenant_branches
-- The EA providers seed uses slug to identify branches when writing back
-- ea_provider_id to tenant_branches.metadata.

ALTER TABLE tenant_branches
    ADD COLUMN IF NOT EXISTS slug TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_branches_tenant_slug
    ON tenant_branches (tenant_id, slug)
    WHERE slug IS NOT NULL;
