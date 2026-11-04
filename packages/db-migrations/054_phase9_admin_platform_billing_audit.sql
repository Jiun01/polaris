-- Migration 054 — Phase 9 schema additions.
-- Phase 9 / PR-1. Ref: docs/Polaris V2/04j_PHASE_9_DEEP.md §3.1.
-- Anchors:
--   - Q-PHASE9-09 (separate platform_admins table; NOT a flag on tenant_users —
--     keeps auth subjects orthogonal and avoids RLS-reasoning bleed).
--   - ADR-030 Amendment (2026-05-20): magic-link only at GA — NO mfa columns.
--     If a second platform_admin joins later, a new amendment + new migration
--     introduces the MFA surface. Append-only.
--   - ADR-028 (invoice format POL-{M|A}-YYMMINDEX): invoice_sequence (from
--     migration 008, NOT recreated here) supplies the monotonic
--     (invoice_type, yy, mm) -> next_index. Phase 9 reuses it via the
--     application-side INSERT...ON CONFLICT DO UPDATE pattern documented in
--     008. The PDF rendered string is held on tenant_invoices.invoice_number.
--   - 00c §12.4 / §12.4.1 (proration mechanics — proration_basis JSONB on
--     tenant_invoices snapshots the inputs at issue time for audit; column
--     ALREADY exists from migration 008, so no add-column needed).
--   - 00c §12.4.2 (sales-quote two-tier framing PDF — sales_quotes stores
--     the prospect's inputs + the rendered PDF coordinates).
--   - Q-PHASE9-03 (audit hash-chain verifier nightly + on-demand — results
--     persist to audit_chain_verifications; admin viewer reads latest row).
--
-- CORRECTION 2026-05-21 (PR-1 follow-up): The first draft of this migration
-- created its own tenant_invoices and invoice_sequence tables — a duplicate
-- of what migration 008 already provides. Fix:
--   - Drop the redundant CREATE TABLE invoice_sequence (008 already has it).
--   - Drop the redundant CREATE TABLE tenant_invoices (008 already has it).
--   - Add ALTER TABLE tenant_invoices ADD COLUMN IF NOT EXISTS for the six
--     columns Phase 9 needs (due_at, marked_paid_by_admin_id, void_reason,
--     pdf_bucket, pdf_object_key, updated_at).
-- 008's tenant_invoices keeps:
--   - period TEXT (legacy; new Phase 9 code uses due_at + issued_at)
--   - amount_myr NUMERIC(12,2) (canonical total — Phase 9 code computes this
--     and supplies a richer breakdown via proration_basis JSONB)
--   - pdf_url TEXT (legacy single-URL; new code uses pdf_bucket+pdf_object_key
--     pair so signed-URL generation can rotate without rewriting the row)
-- The Pydantic shape (TenantInvoice) matches the merged set of columns.
--
-- THREE genuinely-new tables + ALTERs on one existing table:
--
--   (a) platform_admins        [CREATE] — separate auth subject (Q-PHASE9-09)
--   (b) tenant_invoices        [ALTER]  — add Phase 9 columns
--   (c) sales_quotes           [CREATE] — pre-conversion quote PDFs
--   (d) audit_chain_verifications  [CREATE] — verifier run results
--
-- All new tables are service-pool-only (RLS disabled; polaris_anon explicitly
-- REVOKEd). The ALTERed tenant_invoices keeps its existing RLS posture
-- (self-select for the tenant role; service has BYPASSRLS).
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   DROP TABLE IF EXISTS audit_chain_verifications;
--   DROP TABLE IF EXISTS sales_quotes;
--   ALTER TABLE tenant_invoices DROP COLUMN IF EXISTS updated_at;
--   ALTER TABLE tenant_invoices DROP COLUMN IF EXISTS pdf_object_key;
--   ALTER TABLE tenant_invoices DROP COLUMN IF EXISTS pdf_bucket;
--   ALTER TABLE tenant_invoices DROP COLUMN IF EXISTS void_reason;
--   ALTER TABLE tenant_invoices DROP COLUMN IF EXISTS marked_paid_by_admin_id;
--   ALTER TABLE tenant_invoices DROP COLUMN IF EXISTS due_at;
--   DROP TABLE IF EXISTS platform_admins;

-- =====================================================================
-- (a) platform_admins  [CREATE]
-- =====================================================================

CREATE TABLE platform_admins (
  id                       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),

  -- TEXT (not CITEXT — extension not enabled per migration 001).
  -- Email is normalised lowercase at the application layer before INSERT;
  -- the UNIQUE index below enforces no duplicates.
  email                    TEXT         NOT NULL UNIQUE,

  display_name             TEXT         NOT NULL,

  -- Single role at GA. Reserved values for post-GA splits:
  --   'platform_support'  — read-mostly view, no destructive ops
  --   'platform_billing'  — invoice + plan-change scope only
  role                     TEXT         NOT NULL DEFAULT 'platform_admin',

  -- Set FALSE on departure instead of DELETE so FK references from
  -- tenant_invoices.marked_paid_by_admin_id et al survive for audit.
  active                   BOOLEAN      NOT NULL DEFAULT TRUE,

  last_login_at            TIMESTAMPTZ  NULL,

  -- Captured for the IP-bind defence-in-depth per ADR-030 Am.2026-05-20.
  -- Compared against the click IP at /24 prefix in NextAuth session
  -- callback (configurable via ADMIN_MAGIC_LINK_IP_BIND_PREFIX).
  last_login_ip            INET         NULL,

  created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX platform_admins_active_email_idx
  ON platform_admins (email) WHERE active = TRUE;

COMMENT ON TABLE  platform_admins IS
  'Phase 9 — separate auth subject for admin.getpolaris.my; magic-link only per ADR-030 Am.2026-05-20; no MFA at GA.';
COMMENT ON COLUMN platform_admins.last_login_ip IS
  'For IP-bind defence-in-depth per ADR-030 Am.2026-05-20.';
COMMENT ON COLUMN platform_admins.active IS
  'Soft-deactivate (FALSE) instead of DELETE so audit FKs survive departures.';

-- =====================================================================
-- (b) tenant_invoices  [ALTER — add Phase 9 columns]
-- =====================================================================
-- Existing schema from migration 008:
--   id, tenant_id, invoice_number, period, amount_myr, status,
--   pdf_url, proration_basis, issued_at, paid_at, created_at
-- RLS is enabled (FORCE) with tenant_invoices_self_select policy.
-- ALTER below is purely additive; IF NOT EXISTS keeps the migration
-- idempotent for the apply.py test harness.

ALTER TABLE tenant_invoices
  ADD COLUMN IF NOT EXISTS due_at                   TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS marked_paid_by_admin_id  UUID NULL,
  ADD COLUMN IF NOT EXISTS void_reason              TEXT NULL,
  ADD COLUMN IF NOT EXISTS pdf_bucket               TEXT NULL,
  ADD COLUMN IF NOT EXISTS pdf_object_key           TEXT NULL,
  ADD COLUMN IF NOT EXISTS updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Add the FK separately so the platform_admins CREATE above is in scope.
-- Idempotent guard via a DO block — there's no native
-- "ADD CONSTRAINT IF NOT EXISTS" in Postgres < 17.4.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM pg_constraint
     WHERE conname = 'tenant_invoices_marked_paid_by_admin_id_fkey'
  ) THEN
    ALTER TABLE tenant_invoices
      ADD CONSTRAINT tenant_invoices_marked_paid_by_admin_id_fkey
      FOREIGN KEY (marked_paid_by_admin_id)
      REFERENCES platform_admins(id) ON DELETE SET NULL;
  END IF;
END $$;

-- Overdue-only index used by the daily 09:00 MYT overdue-cron (PR-7).
CREATE INDEX IF NOT EXISTS tenant_invoices_overdue_idx
  ON tenant_invoices (due_at) WHERE status = 'overdue';

COMMENT ON COLUMN tenant_invoices.due_at IS
  'Phase 9 — invoice due date; flips status to overdue via daily cron when NOW() > due_at AND status = ''open''.';
COMMENT ON COLUMN tenant_invoices.marked_paid_by_admin_id IS
  'Phase 9 — which platform_admin clicked Mark Paid in admin UI. NULL when payment was reconciled automatically.';
COMMENT ON COLUMN tenant_invoices.pdf_bucket IS
  'Phase 9 — Supabase Storage bucket for the issued PDF. Pairs with pdf_object_key. Replaces legacy single pdf_url; old rows keep pdf_url populated.';

-- =====================================================================
-- (c) sales_quotes  [CREATE]
-- =====================================================================

CREATE TABLE sales_quotes (
  id                       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),

  -- POL-Q-YYMMINDEX, parallel namespace to invoice_sequence per
  -- 04j §3.1. Stored as a separate column for human-facing reference;
  -- monotonicity tracked in application code (PR-9) using the same
  -- invoice_sequence table (008) but with invoice_type = 'Q'.
  -- Format matches the existing invoice_number CHECK: no dash between
  -- yymm and index (POL-Q-26060042, not POL-Q-2606-0042).
  quote_code               TEXT         NOT NULL UNIQUE
                             CHECK (quote_code ~ '^POL-Q-[0-9]{2}[0-9]{2}[0-9]+$'),

  prospect_email           TEXT         NULL,
  prospect_company         TEXT         NULL,

  target_tier              TEXT         NOT NULL
                             CHECK (target_tier IN ('starter', 'pro', 'ultra', 'enterprise')),
  target_billing_cycle     TEXT         NOT NULL
                             CHECK (target_billing_cycle IN ('monthly', 'annual')),
  intended_signup_date     DATE         NOT NULL,

  -- Inputs for re-render: {prorate_basis, days_remaining_in_period,
  -- headline_price, prorated_invoice, prorated_quota, renderer_version}.
  -- Combined with the bucketed PDF bytes this gives byte-stable
  -- re-downloads per Q-PHASE9-05 — the bucket bytes are authoritative;
  -- proration_inputs lets us regenerate for diff against a new
  -- renderer_version if we want to audit a template change.
  proration_inputs         JSONB        NOT NULL DEFAULT '{}'::jsonb,

  -- Generated once at creation per Q-PHASE9-05; durable across re-downloads.
  pdf_bucket               TEXT         NOT NULL,
  pdf_object_key           TEXT         NOT NULL,

  generated_by_admin_id    UUID         NOT NULL REFERENCES platform_admins(id) ON DELETE RESTRICT,

  -- Set on conversion via POST /api/admin/quotes/{id}/convert/{tenant_id}.
  -- NULL while the quote is unconverted.
  converted_to_tenant_id   UUID         NULL REFERENCES tenants(id) ON DELETE SET NULL,
  converted_at             TIMESTAMPTZ  NULL,

  -- Default 30 days from generation. UI warns on conversion past expiry.
  expires_at               TIMESTAMPTZ  NOT NULL,

  created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX sales_quotes_unconverted_idx
  ON sales_quotes (created_at DESC) WHERE converted_to_tenant_id IS NULL;

CREATE INDEX sales_quotes_prospect_email_idx
  ON sales_quotes (LOWER(prospect_email))
  WHERE prospect_email IS NOT NULL;

COMMENT ON TABLE sales_quotes IS
  'Phase 9 — pre-conversion sales quotes per 00c §12.4.2; PDF byte-stable in Supabase Storage per Q-PHASE9-05.';

-- =====================================================================
-- (d) audit_chain_verifications  [CREATE]
-- =====================================================================

CREATE TABLE audit_chain_verifications (
  id                       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),

  checked_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  -- intact | broken | errored
  status                   TEXT         NOT NULL
                             CHECK (status IN ('intact', 'broken', 'errored')),

  rows_scanned             BIGINT       NOT NULL,
  duration_ms              INT          NOT NULL,

  -- All NULL when status='intact'; populated when status='broken'.
  broken_at_id             UUID         NULL,
  broken_at_created_at     TIMESTAMPTZ  NULL,
  broken_prev_hash         BYTEA        NULL,  -- expected (recomputed)
  broken_actual_hash       BYTEA        NULL,  -- found in the row

  -- 'scheduled' (04:00 MYT cron) | 'on_demand' (admin button click)
  triggered_by             TEXT         NOT NULL DEFAULT 'scheduled'
                             CHECK (triggered_by IN ('scheduled', 'on_demand')),
  triggered_by_admin_id    UUID         NULL REFERENCES platform_admins(id) ON DELETE SET NULL,

  notes                    TEXT         NULL
);

CREATE INDEX audit_chain_verifications_recent_idx
  ON audit_chain_verifications (checked_at DESC);

CREATE INDEX audit_chain_verifications_broken_idx
  ON audit_chain_verifications (checked_at DESC) WHERE status = 'broken';

COMMENT ON TABLE audit_chain_verifications IS
  'Phase 9 — nightly 04:00 MYT verifier results + on-demand re-verifies. Admin viewer reads last row for the banner; broken status fires Sentry alert.';

-- =====================================================================
-- Grants — service-pool ONLY for the new tables. polaris_anon explicitly
-- REVOKEd to make the boundary unambiguous. tenant_invoices grants are
-- unchanged from migration 008 (RLS still in force).
-- =====================================================================

GRANT ALL ON platform_admins              TO polaris_service;
GRANT ALL ON sales_quotes                 TO polaris_service;
GRANT ALL ON audit_chain_verifications    TO polaris_service;

REVOKE ALL ON platform_admins              FROM polaris_anon;
REVOKE ALL ON sales_quotes                 FROM polaris_anon;
REVOKE ALL ON audit_chain_verifications    FROM polaris_anon;

INSERT INTO schema_migrations (filename) VALUES ('054_phase9_admin_platform_billing_audit.sql')
ON CONFLICT (filename) DO NOTHING;
