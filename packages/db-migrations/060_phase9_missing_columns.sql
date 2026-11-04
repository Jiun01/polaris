-- Migration 060 — Phase 9 missing columns (display_name on tenants, mrr_myr on tenant_billing).
--
-- These columns are referenced by admin_tenants.py and admin_financials.py but were
-- absent from migrations 002 and 008. Added here with IF NOT EXISTS guards so the
-- migration is idempotent for apply.py and for DBs that already have them from a
-- manual ALTER during Phase 9 development.
--
-- Anchors:
--   - admin_tenants.py POST / — INSERT INTO tenants (slug, display_name, ...)
--   - admin_financials.py — compute_and_persist reads SUM(mrr_myr) from tenant_billing
--   - mrr_rollup.py — SELECT COALESCE(SUM(mrr_myr), 0) FROM tenant_billing WHERE status='active'

-- (a) tenants.display_name — human-readable name set on wizard create.
--     NOT NULL DEFAULT '' keeps existing rows valid; the wizard always
--     supplies a non-empty value for new tenants.
ALTER TABLE tenants
  ADD COLUMN IF NOT EXISTS display_name TEXT NOT NULL DEFAULT '';

-- (b) tenant_billing.mrr_myr — monthly recurring revenue in MYR, maintained
--     by the admin billing path. SUM'd by the MRR rollup service. Defaults
--     to 0 so existing rows survive the migration without a data-fill.
ALTER TABLE tenant_billing
  ADD COLUMN IF NOT EXISTS mrr_myr NUMERIC(12, 2) NOT NULL DEFAULT 0;

INSERT INTO schema_migrations (filename)
VALUES ('060_phase9_missing_columns.sql')
ON CONFLICT (filename) DO NOTHING;
