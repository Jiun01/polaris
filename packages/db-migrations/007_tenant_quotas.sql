-- Migration 007 — tenant_quotas (16-column locked shape).
-- Phase 1 / PR-5. Ref: docs/Polaris V2/05_DATA_MODEL_AND_API.md §4.2 + 04b §3.4.
-- Anchors: ADR-015 Amendment 3rd (channels_per_kind dropped; allowed_channel_kinds[]),
--          ADR-026 (4 tier model: starter/pro/ultra/enterprise + add-ons),
--          ADR-017 (quota dimensions locked).
--
-- 1:1 with tenants (PRIMARY KEY = tenant_id, ON DELETE CASCADE). Each tenant
-- has exactly one row of quotas; tier-default quotas are populated by Phase 6
-- (sales-led tenant provisioning) — PR-5 only ships the schema.
--
-- All quota columns are NOT NULL to force a conscious seeding decision per
-- tier. Defaults are reserved for the two columns that have a single safe
-- value across all tiers (vision_calls_per_month = 0 at GA all tiers per
-- ADR-026; agent_seats_included/extra defaults match Starter's tier).
--
-- bot_replies_period_cap is the period-scoped working column: re-seeded each
-- billing cycle from `bot_replies` with proration for mid-cycle signup +
-- upgrade. Read by the Redis incr counter as the cap; written by the billing
-- cycle job (Phase 10).
--
-- RLS posture: tenant reads its own row. Modifications are platform-managed
-- (admin sets quotas via service_pool). No INSERT/UPDATE/DELETE policies =
-- those operations deny for polaris_anon (RLS without policy = empty).
--
-- Rollback (local only):
--   DROP TABLE IF EXISTS tenant_quotas;

CREATE TABLE tenant_quotas (
  tenant_id                    UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,

  -- Customer-facing meters (7) — visible in portal billing/usage pages.
  bot_replies                  INT     NOT NULL,
  vision_calls_per_month       INT     NOT NULL DEFAULT 0,
  conversation_retention_days  INT     NOT NULL,
  allowed_channel_kinds        TEXT[]  NOT NULL,
  outbound_broadcast_enabled   BOOLEAN NOT NULL,
  multi_branch_enabled         BOOLEAN NOT NULL,
  knowledge_chunks_total_mb    INT     NOT NULL,

  -- Internal cost / capacity ceilings (7) — invisible to tenant, enforced
  -- by the LLM router, ingestion worker, broadcast worker, booking flow.
  rate_limit_per_minute        INT     NOT NULL,
  monthly_llm_tokens_in        INT     NOT NULL,
  monthly_llm_tokens_out       INT     NOT NULL,
  monthly_ingestion_pages      INT     NOT NULL,
  monthly_broadcast_recipients INT     NOT NULL,
  monthly_bookings             INT     NOT NULL,
  agent_seats_included         INT     NOT NULL DEFAULT 1,
  agent_seats_extra            INT     NOT NULL DEFAULT 0,

  -- Period-scoped working column (1) — Redis-seeded each cycle.
  bot_replies_period_cap       INT     NOT NULL,

  created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE tenant_quotas ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_quotas FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_quotas_self_select ON tenant_quotas
  FOR SELECT
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

INSERT INTO schema_migrations (filename) VALUES ('007_tenant_quotas.sql')
ON CONFLICT (filename) DO NOTHING;
