-- Migration 008 — tenant_billing + tenant_invoices + invoice_sequence.
-- Phase 1 / PR-5. Ref: docs/Polaris V2/05_DATA_MODEL_AND_API.md §2.1 + 04b §3.4.
-- Anchors: ADR-028 (invoice format POL-{M|A}-YYMMINDEX; POLARIS TECHNOLOGIES
--          SSM 202603066744 / AS0511535-P), ADR-032 Am (kb_mb_cap_override
--          for Enterprise SOWs), ADR-036 (Curlec direct-debit migration
--          trigger), ADR-037 (SST tripwire at RM 35k MRR).
--
-- Phase 1 scaffolds the tables only. The billing flow (invoice generation,
-- payment status transitions, dunning ramp per ADR-049) lands in Phase 10.
--
-- Rollback (local only):
--   DROP TABLE IF EXISTS tenant_invoices CASCADE;
--   DROP TABLE IF EXISTS invoice_sequence;
--   DROP TABLE IF EXISTS tenant_billing CASCADE;

-- tenant_billing — 1:1 with tenants. Billing-entity facts (SSM/TIN for tax
-- compliance) live here, not on `tenants` itself, so the tenant row stays
-- product-shaped and the billing row stays accountant-shaped.
CREATE TABLE tenant_billing (
  tenant_id              UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  status                 TEXT    NOT NULL DEFAULT 'active',
  plan_tier              TEXT    NOT NULL,    -- mirror of tenants.plan_tier for billing queries
  kb_mb_cap_override     INT     NULL,        -- Enterprise SOW override (NULL = use tenant_quotas)
  company_name           TEXT    NULL,
  ssm                    TEXT    NULL,        -- Suruhanjaya Syarikat Malaysia registration #
  tin                    TEXT    NULL,        -- Tax Identification Number
  address                TEXT    NULL,
  billing_contact_email  TEXT    NULL,
  billing_contact_phone  TEXT    NULL,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE tenant_billing ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_billing FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_billing_self_select ON tenant_billing
  FOR SELECT
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- tenant_invoices — append-mostly. `invoice_number` is the customer-facing
-- identifier; format enforced by app + a CHECK to catch silent format drift.
CREATE TABLE tenant_invoices (
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id              UUID NOT NULL REFERENCES tenants(id),
  invoice_number         TEXT NOT NULL UNIQUE
                                CHECK (invoice_number ~ '^POL-[MA]-[0-9]{2}[0-9]{2}[0-9]+$'),
  period                 TEXT NOT NULL,                -- e.g. '2026-05' or '2026' for annual
  amount_myr             NUMERIC(12, 2) NOT NULL,
  status                 TEXT    NOT NULL DEFAULT 'open',  -- open|paid|overdue|void
  pdf_url                TEXT    NULL,                -- supabase_storage_path
  proration_basis        JSONB   NULL,                -- mid-cycle adjustment audit per 00c §12.4.1
  issued_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  paid_at                TIMESTAMPTZ NULL,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX tenant_invoices_tenant_issued_idx
  ON tenant_invoices (tenant_id, issued_at DESC);

ALTER TABLE tenant_invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_invoices FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_invoices_self_select ON tenant_invoices
  FOR SELECT
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- invoice_sequence — platform-scoped (no RLS). Auto-advances per
-- (invoice_type, yy, mm). Application-side update pattern:
--   INSERT INTO invoice_sequence (invoice_type, yy, mm, next_index)
--   VALUES ($1, $2, $3, 1)
--   ON CONFLICT (invoice_type, yy, mm) DO UPDATE
--     SET next_index = invoice_sequence.next_index + 1
--   RETURNING next_index;
CREATE TABLE invoice_sequence (
  invoice_type  CHAR(1)   NOT NULL CHECK (invoice_type IN ('M', 'A')),  -- monthly / annual
  yy            SMALLINT  NOT NULL CHECK (yy BETWEEN 0 AND 99),
  mm            SMALLINT  NOT NULL CHECK (mm BETWEEN 1 AND 12),
  next_index    INT       NOT NULL DEFAULT 1,
  PRIMARY KEY (invoice_type, yy, mm)
);
-- No RLS — platform_admin / service_pool only via 014_phase1_pr5_grants.sql.

INSERT INTO schema_migrations (filename) VALUES ('008_tenant_billing.sql')
ON CONFLICT (filename) DO NOTHING;
