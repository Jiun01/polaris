-- Migration 071: add metadata + updated_at to tenant_branches
-- Required by seed_e2e_ea_providers.py which writes ea_provider_id
-- back to tenant_branches.metadata after creating EA providers.

ALTER TABLE tenant_branches
    ADD COLUMN IF NOT EXISTS metadata  JSONB,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
