-- Migration 087 — widen two over-narrow CHECK constraints found by live browser QA.
--
-- (1) tenants_suspended_reason_check only allowed 'quota_exhausted' | 'payment_overdue'
--     | NULL — but the admin "Suspend" button does a MANUAL suspend that writes
--     suspended_reason = 'manual' (routers/admin_tenants.py::suspend_tenant), so every
--     manual suspend 500'd with a CheckViolationError. Add 'manual'.
--
-- (2) invoice_sequence_invoice_type_check only allowed 'M' | 'A' — but sales quotes
--     reuse the sequencer with invoice_type = 'Q' (services/billing/invoice_sequencer.py
--     ::next_quote_number, intended since migration 054 §c but never reflected in the
--     live constraint), so "Generate quote" 500'd. Add 'Q'.
--
-- Both are widen-only (no data rewrite); idempotent via DROP IF EXISTS + re-add.

ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_suspended_reason_check;
ALTER TABLE tenants ADD CONSTRAINT tenants_suspended_reason_check
    CHECK (
        suspended_reason IS NULL
        OR suspended_reason = ANY (ARRAY['quota_exhausted', 'payment_overdue', 'manual'])
    );

ALTER TABLE invoice_sequence DROP CONSTRAINT IF EXISTS invoice_sequence_invoice_type_check;
ALTER TABLE invoice_sequence ADD CONSTRAINT invoice_sequence_invoice_type_check
    CHECK (invoice_type = ANY (ARRAY['M'::bpchar, 'A'::bpchar, 'Q'::bpchar]));

INSERT INTO schema_migrations (filename) VALUES ('087_qa_constraint_widening.sql')
ON CONFLICT (filename) DO NOTHING;
