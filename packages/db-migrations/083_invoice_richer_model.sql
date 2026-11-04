-- Migration 083 — richer invoice model (Phase 9 reconciliation, 2026-06-01).
--
-- The admin billing UI (TenantInvoice TS type, InvoiceTable, IssueInvoiceForm,
-- and the invoice detail page) was built for a richer invoice model — an
-- invoice_kind, a subtotal/tax/total breakdown, and a period_start/period_end
-- range — but the backend + DB only ever had `amount_myr` + `period` (TEXT
-- 'YYYY-MM'). That mismatch made the invoice detail page 500 for every invoice
-- (invoice_kind.replace on undefined) and the issue form 422. This migration
-- adds the richer columns so the UI works as designed.
--
-- `amount_myr` and `period` are KEPT and populated in lockstep (amount_myr =
-- total_myr; period = to_char(period_start,'YYYY-MM')) for backward compat with
-- the daily invoice_overdue job and the `period LIKE` month filter.
--
-- Table-level grants (migration 014: GRANT ALL ... polaris_service, GRANT SELECT
-- ... polaris_anon) automatically cover new columns — no re-grant needed.
-- Append-only + idempotent (IF NOT EXISTS / guarded backfill).

ALTER TABLE tenant_invoices
  ADD COLUMN IF NOT EXISTS invoice_kind  TEXT,
  ADD COLUMN IF NOT EXISTS period_start  DATE,
  ADD COLUMN IF NOT EXISTS period_end    DATE,
  ADD COLUMN IF NOT EXISTS subtotal_myr  NUMERIC(12, 2),
  ADD COLUMN IF NOT EXISTS tax_myr       NUMERIC(12, 2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS total_myr     NUMERIC(12, 2);

-- Backfill existing rows from amount_myr + period (TEXT 'YYYY-MM' or 'YYYY').
UPDATE tenant_invoices
   SET invoice_kind = COALESCE(invoice_kind, 'monthly_subscription'),
       subtotal_myr = COALESCE(subtotal_myr, amount_myr),
       total_myr    = COALESCE(total_myr, amount_myr),
       period_start = COALESCE(
                        period_start,
                        CASE
                          WHEN period ~ '^\d{4}-\d{2}$' THEN to_date(period || '-01', 'YYYY-MM-DD')
                          WHEN period ~ '^\d{4}$'        THEN to_date(period || '-01-01', 'YYYY-MM-DD')
                          ELSE issued_at::date
                        END),
       period_end   = COALESCE(
                        period_end,
                        CASE
                          WHEN period ~ '^\d{4}-\d{2}$' THEN (to_date(period || '-01', 'YYYY-MM-DD') + INTERVAL '1 month' - INTERVAL '1 day')::date
                          WHEN period ~ '^\d{4}$'        THEN (to_date(period || '-01-01', 'YYYY-MM-DD') + INTERVAL '1 year'  - INTERVAL '1 day')::date
                          ELSE issued_at::date
                        END)
 WHERE invoice_kind IS NULL
    OR subtotal_myr IS NULL
    OR total_myr   IS NULL
    OR period_start IS NULL
    OR period_end  IS NULL;

-- Constrain invoice_kind to the four UI kinds (added AFTER backfill so existing
-- rows pass). NULL is permitted by a CHECK, but the API always sets it.
ALTER TABLE tenant_invoices DROP CONSTRAINT IF EXISTS tenant_invoices_invoice_kind_check;
ALTER TABLE tenant_invoices
  ADD CONSTRAINT tenant_invoices_invoice_kind_check
  CHECK (invoice_kind IS NULL OR invoice_kind IN (
    'monthly_subscription', 'annual_subscription', 'plan_change_proration', 'addon_seat'
  ));

INSERT INTO schema_migrations (filename) VALUES ('083_invoice_richer_model.sql')
ON CONFLICT (filename) DO NOTHING;
