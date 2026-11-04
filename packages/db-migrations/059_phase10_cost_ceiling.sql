-- Migration 059 — Phase 10 PR-4: per-tenant LLM cost ceiling.
-- ADR-035 (Sentry per-tenant 5×10min alert), ADR-026 (plan tier caps).
--
-- Adds a single nullable column to tenant_quotas that the nightly
-- cost_ceiling_check job reads.  NULL = no ceiling (default at GA so
-- existing tenants are unaffected until the operator explicitly sets one
-- via the admin quotas override endpoint).
--
-- Alert thresholds (evaluated nightly by cost_ceiling_check.py):
--   ≥ 75%  → email to ADMIN_EMAIL_TO
--   ≥ 100% → Sentry fatal page + auto-disable Cohere rerank
--
-- Rollback (local only):
--   ALTER TABLE tenant_quotas DROP COLUMN IF EXISTS llm_cost_ceiling_myr;

ALTER TABLE tenant_quotas
  ADD COLUMN IF NOT EXISTS llm_cost_ceiling_myr NUMERIC(10, 2);

COMMENT ON COLUMN tenant_quotas.llm_cost_ceiling_myr IS
  'Hard LLM cost ceiling per calendar month in MYR.  '
  'NULL = no ceiling (default).  '
  'At 75 % → warning email to platform admin.  '
  'At 100 % → Sentry fatal page + cohere_rerank_enabled set to FALSE.  '
  'Set by platform_admin via POST /api/admin/quotas/{tenant_id}/override '
  '(column name: llm_cost_ceiling_myr).';
