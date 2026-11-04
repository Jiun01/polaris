-- Migration 002 — tenants table + tenant_status + tenant_plan_tier enums + RLS.
-- Phase 1 / PR-1. Ref: docs/Polaris V2/04b_PHASE_1_DEEP.md §3.1 + 05_DATA_MODEL_AND_API.md §4.1.
-- Anchors: ADR-002 (RLS-by-default), ADR-018 (suspended_reason quota), ADR-026 (plan tiers),
--          ADR-027 (demo_code), ADR-029 (lifecycle), ADR-049 (suspended_reason payment).
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   DROP TABLE IF EXISTS tenants CASCADE;
--   DROP TYPE  IF EXISTS tenant_plan_tier;
--   DROP TYPE  IF EXISTS tenant_status;

CREATE TYPE tenant_status     AS ENUM ('active', 'suspended', 'deleted');
CREATE TYPE tenant_plan_tier  AS ENUM ('starter', 'pro', 'ultra', 'enterprise');

CREATE TABLE tenants (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug                        TEXT NOT NULL UNIQUE,
  status                      tenant_status NOT NULL DEFAULT 'active',
  suspended_reason            TEXT NULL CHECK (
                                suspended_reason IN ('quota_exhausted', 'payment_overdue')
                                OR suspended_reason IS NULL
                              ),
  plan_tier                   tenant_plan_tier NOT NULL DEFAULT 'starter',
  timezone                    TEXT NOT NULL DEFAULT 'Asia/Kuala_Lumpur',
  default_country_code        TEXT NOT NULL DEFAULT 'MY',
  demo_code                   TEXT UNIQUE,
  chatwoot_account_id         INT  UNIQUE,
  system_prompt               TEXT,
  persona_tone                TEXT,
  ai_disclosure_enabled       BOOLEAN NOT NULL DEFAULT TRUE,
  business_hours              JSONB   NOT NULL DEFAULT '{}'::jsonb,
  handoff_message_in_hours    TEXT,
  handoff_message_after_hours TEXT,
  chunk_size                  INT     NOT NULL DEFAULT 512,
  chunk_overlap               INT     NOT NULL DEFAULT 80,
  rag_top_k                   INT     NOT NULL DEFAULT 5,
  rag_confidence_threshold    NUMERIC          DEFAULT 0.3,
  cohere_rerank_enabled       BOOLEAN,
  last_message_at             TIMESTAMPTZ,
  handoff_at                  TIMESTAMPTZ,
  settings                    JSONB   NOT NULL DEFAULT '{}'::jsonb,
  subscription_start          TIMESTAMPTZ,
  subscription_end            TIMESTAMPTZ,
  billing_status              TEXT    NOT NULL DEFAULT 'active',
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX tenants_status_idx    ON tenants (status);
CREATE INDEX tenants_plan_tier_idx ON tenants (plan_tier);

ALTER TABLE tenants ENABLE  ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE   ROW LEVEL SECURITY;

-- A tenant can see and update its own row. INSERT/DELETE are platform-only
-- (handled via service_pool BYPASSRLS in PR-2+). No INSERT/DELETE policy here
-- = no INSERT/DELETE from anon_pool = defense-in-depth per ADR-002.
CREATE POLICY tenants_self_select ON tenants
  FOR SELECT
  USING (id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenants_self_update ON tenants
  FOR UPDATE
  USING (id = current_setting('app.current_tenant_id', true)::uuid);

INSERT INTO schema_migrations (filename) VALUES ('002_tenants.sql')
ON CONFLICT (filename) DO NOTHING;
