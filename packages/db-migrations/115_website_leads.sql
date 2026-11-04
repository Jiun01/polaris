-- Migration 115 — website_leads: marketing-site form submissions.
-- Ref: ../docs/internal-ops/PRPs/plans/completed/forms-gas-to-cloudflare-worker.plan.md
--
-- Captures leads from the four getpolaris.my marketing forms (demo / pricing /
-- contact / website-request) after the Google Apps Script backend was retired.
-- A Cloudflare Pages Function validates each submission and writes the row here
-- via the Supabase REST API as the Supabase `service_role`; the Polaris admin
-- API reads + updates it as `polaris_service` (BYPASSRLS).
--
-- Posture (mirrors 097 email_drafts / 107 service-only tables):
--   * Service-path-only table — NO `polaris_anon` grant.
--   * RLS ENABLED with no policy → denies Supabase's PostgREST `anon` /
--     `authenticated` roles (the public anon key cannot read leads/PII). Both
--     `polaris_service` and Supabase `service_role` bypass RLS, so the app +
--     the Function are unaffected.
--   * `service_role` is granted explicitly because — unlike polaris_service — it
--     is NOT the table owner and has no default privileges on a table created by
--     the migrator role. The grant is guarded by an IF EXISTS check so this
--     migration still applies on the local/CI Postgres (postgres:17), which has
--     no Supabase `service_role`.
--
-- Lead lifecycle: status is binary `new` (to-do inbox) → `contacted` (archive);
-- marking contacted is one-way for v1. contacted_at / contacted_by record who
-- actioned it (platform_admins.id).
--
-- Idempotent: CREATE TABLE / INDEX use IF NOT EXISTS; grant guarded. Apply order
-- via schema_migrations.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   DROP TABLE IF EXISTS website_leads;

CREATE TABLE IF NOT EXISTS website_leads (
  id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  form_type      TEXT         NOT NULL CHECK (form_type IN ('demo', 'pricing', 'contact', 'website')),
  name           TEXT         NOT NULL,
  email          TEXT         NOT NULL,
  phone          TEXT,
  business_name  TEXT,
  industry       TEXT,
  message        TEXT,
  details        JSONB        NOT NULL DEFAULT '{}'::jsonb,
  source_ip      INET,
  user_agent     TEXT,
  status         TEXT         NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'contacted')),
  contacted_at   TIMESTAMPTZ,
  contacted_by   UUID         REFERENCES platform_admins (id) ON DELETE SET NULL,
  created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Cursor pagination (created_at, id) DESC.
CREATE INDEX IF NOT EXISTS website_leads_created_idx
  ON website_leads (created_at DESC, id DESC);

-- Serves every admin chip: status (+ optional form_type), newest first.
CREATE INDEX IF NOT EXISTS website_leads_status_type_idx
  ON website_leads (status, form_type, created_at DESC);

CREATE INDEX IF NOT EXISTS website_leads_email_idx
  ON website_leads (email);

-- Service-only: deny the Supabase PostgREST anon/authenticated roles.
ALTER TABLE website_leads ENABLE ROW LEVEL SECURITY;

-- Polaris admin API (read + mark-contacted) — BYPASSRLS service pool.
GRANT SELECT, INSERT, UPDATE ON website_leads TO polaris_service;
-- polaris_anon: no grant (service-path only; see header).

-- Supabase service_role (the Cloudflare Function's PostgREST write key). Guarded
-- so the migration is a no-op on clusters without Supabase roles (local / CI).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    GRANT SELECT, INSERT ON website_leads TO service_role;
  END IF;
END
$$;

INSERT INTO schema_migrations (filename) VALUES ('115_website_leads.sql')
ON CONFLICT (filename) DO NOTHING;
