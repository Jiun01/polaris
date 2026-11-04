-- Migration 107 — enable RLS on service-only + platform tables (defense-in-depth).
-- Ref: ADR-002 (dual-pool RLS isolation).
--
-- There is NO tenant-isolation gap to fix here: every table reachable by the
-- NOBYPASSRLS `polaris_anon` pool already has RLS + tenant policies. This
-- migration closes a DIFFERENT surface — Supabase's built-in `anon` /
-- `authenticated` roles (the PostgREST / anon-key REST API) can read
-- RLS-disabled tables in `public`. The tables below are granted ONLY to
-- `polaris_service` (BYPASSRLS), so enabling RLS with no policy is a no-op for
-- the app (the service role bypasses RLS) while denying the PostgREST roles —
-- and it clears the Supabase "RLS disabled in public" linter warnings.
--
-- `email_drafts` is tenant-scoped (per-tenant email content / PII) and currently
-- service-only; it additionally gets a real tenant-isolation policy so a future
-- `GRANT ... TO polaris_anon` (e.g. a portal screen) is safe by construction.
--
-- `flow_templates` is an intentional GLOBAL read-only catalogue granted to
-- `polaris_anon`; it gets a permissive SELECT policy so the catalogue stays
-- readable while the table reports RLS-enabled.
--
-- `schema_migrations` is deliberately EXCLUDED: it holds no sensitive data (only
-- migration filenames) and the apply.py applier INSERTs into it on every run —
-- enabling RLS there risks blocking that bookkeeping write if the applier role
-- is ever non-superuser. Cosmetic linter warning only.

-- ── Credential / auth tables (highest priority — hold secrets) ───────────────
ALTER TABLE admin_magic_link_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE magic_link_tokens       ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_admins         ENABLE ROW LEVEL SECURITY;

-- ── Platform financials / ops telemetry (admin + service only) ───────────────
ALTER TABLE platform_fixed_costs      ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_mrr_snapshots    ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_health           ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_quotes              ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_chain_verifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingestion_runs            ENABLE ROW LEVEL SECURITY;
ALTER TABLE meta_api_usage            ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoice_sequence          ENABLE ROW LEVEL SECURITY;

-- ── email_drafts: tenant-scoped → enable + FORCE + tenant isolation policy ───
-- FORCE so the policy applies even to the table owner; polaris_service bypasses
-- via BYPASSRLS so the email-assist worker is unaffected.
ALTER TABLE email_drafts ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_drafts FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS email_drafts_tenant_isolation ON email_drafts;
CREATE POLICY email_drafts_tenant_isolation ON email_drafts
  FOR ALL
  USING       (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK  (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- ── flow_templates: intentional global catalogue → enable + permissive read ──
ALTER TABLE flow_templates ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS flow_templates_public_read ON flow_templates;
CREATE POLICY flow_templates_public_read ON flow_templates
  FOR SELECT
  USING (true);

INSERT INTO schema_migrations (filename) VALUES ('107_enable_rls_service_tables.sql')
ON CONFLICT (filename) DO NOTHING;
