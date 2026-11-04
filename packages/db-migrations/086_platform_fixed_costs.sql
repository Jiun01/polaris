-- Migration 086 — platform_fixed_costs: operator-entered fixed/global cost pool.
--
-- The cost dashboard historically attributed EVERY cost per tenant (pulled from
-- Grafana Mimir), including Supabase storage — but Supabase/Redis/VPS/Grafana/
-- Sentry/domains are FIXED platform overhead that doesn't vary per tenant, so
-- per-tenant attribution distorted unit economics. The cost model now splits:
--   * per-tenant VARIABLE (Polaris COGS): Anthropic + Cohere + OpenRouter
--   * platform FIXED pool: these operator-entered line items (kept separate,
--     NOT amortized into per-tenant cost).
--
-- WhatsApp conversation fees are client-borne (each tenant uses their own WABA;
-- Meta bills them directly), so there is NO per-tenant WA cost. The ONE
-- exception is Polaris's shared demo number (ADR-039), whose Meta fees Polaris
-- pays — represented here as a single 'Demo WhatsApp' fixed line item.
--
-- Nothing measures these values, so they are operator-entered + edited in the
-- admin Cost page. Seeded with the common line-item NAMES at RM 0; the operator
-- fills in real amounts. Read + written exclusively by the service pool (admin
-- routes); no RLS needed (platform-global, not tenant-scoped). Append-only.

CREATE TABLE IF NOT EXISTS platform_fixed_costs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    label       TEXT          NOT NULL,
    category    TEXT,
    monthly_myr NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (monthly_myr >= 0),
    active      BOOLEAN       NOT NULL DEFAULT TRUE,
    notes       TEXT,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON platform_fixed_costs TO polaris_service;

-- Seed the common line items at RM 0 (operator edits amounts in the UI).
INSERT INTO platform_fixed_costs (label, category, monthly_myr) VALUES
    ('Supabase plan',     'infra',         0),
    ('Redis',             'infra',         0),
    ('VPS / hosting',     'infra',         0),
    ('Grafana Cloud',     'observability', 0),
    ('Sentry',            'observability', 0),
    ('Domains',           'infra',         0),
    ('Demo WhatsApp',     'marketing',     0);

INSERT INTO schema_migrations (filename) VALUES ('086_platform_fixed_costs.sql')
ON CONFLICT (filename) DO NOTHING;
