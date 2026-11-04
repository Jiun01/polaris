-- Migration 014 — GRANTs for PR-5 tables.
-- Phase 1 / PR-5 follow-up to migration 010 (roles).
--
-- Tables introduced in this PR:
--   tenant_quotas    — tenant reads own; admin manages
--   tenant_billing   — tenant reads own; admin manages
--   tenant_invoices  — tenant reads own; admin creates
--   invoice_sequence — platform only (no anon access)
--   demo_sessions    — tenant_id-scoped (= polaris-internal in practice)
--   audience_members — tenant manages own
--
-- This file lives separately because the roles polaris_anon / polaris_service
-- are created by migration 010, which sorts AFTER 007/008/008b/009/013 in
-- the filename-ordered apply sequence. Inline grants in 007/008/008b/009/013
-- would fail on a fresh DB. Same pattern as 011_secrets_grants.sql and
-- 012_audit_grants.sql.
--
-- Rollback (local only):
--   REVOKE ALL ON tenant_quotas, tenant_billing, tenant_invoices,
--                 invoice_sequence, demo_sessions, audience_members
--     FROM polaris_anon, polaris_service;

-- tenant_quotas — tenant reads, admin writes.
GRANT SELECT                          ON tenant_quotas    TO polaris_anon;
GRANT ALL                             ON tenant_quotas    TO polaris_service;

-- tenant_billing — same pattern.
GRANT SELECT                          ON tenant_billing   TO polaris_anon;
GRANT ALL                             ON tenant_billing   TO polaris_service;

-- tenant_invoices — same pattern (tenant can list own invoices in portal).
GRANT SELECT                          ON tenant_invoices  TO polaris_anon;
GRANT ALL                             ON tenant_invoices  TO polaris_service;

-- invoice_sequence — platform-only. polaris_anon never touches it.
GRANT ALL                             ON invoice_sequence TO polaris_service;

-- demo_sessions — full DML for both roles. The demo router runs as
-- polaris_anon with app.current_tenant_id = polaris-internal UUID; the
-- nightly purge sweeper runs as polaris_service.
GRANT SELECT, INSERT, UPDATE, DELETE  ON demo_sessions    TO polaris_anon;
GRANT ALL                             ON demo_sessions    TO polaris_service;

-- audience_members — tenant manages own records (CSV upload, opt-out flips).
GRANT SELECT, INSERT, UPDATE, DELETE  ON audience_members TO polaris_anon;
GRANT ALL                             ON audience_members TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('014_phase1_pr5_grants.sql')
ON CONFLICT (filename) DO NOTHING;
