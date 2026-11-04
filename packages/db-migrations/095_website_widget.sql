-- Migration 095 — website launcher widget (off-Meta channels M3/M4).
-- Ref: .claude/prds/off-meta-channels.prd.md (Milestones 3-4).
--
-- The widget is a click-to-chat LAUNCHER, not a hosted chat: a floating bubble
-- the tenant embeds on their own site that routes visitors INTO the tenant's
-- real channels (Telegram / WhatsApp / ...) via direct https deep links
-- (t.me / wa.me). One table:
--
--   tenant_widget_config — one row per tenant. Holds the public widget_key
--   (opaque, non-secret — embedded in the tenant's <script>), the enabled flag,
--   a greeting, and an ordered JSONB list of channel entries
--   ({kind, label, target}). Editable in-portal (M4) WITHOUT re-embedding.
--   RLS-scoped to the tenant (portal CRUD via the anon pool); the public bubble
--   config read goes through the service pool (no tenant context at render).
--
-- READ-ONLY design: the bubble fetches this config once per page load and links
-- each channel DIRECTLY at its https target (t.me / wa.me). There is NO server
-- redirect and NO click-event write — a direct <a href> tap is both lighter
-- (no public write endpoint to rate-limit) AND more reliable on mobile (a server
-- 302 breaks the iOS Universal-Link user-gesture chain; a direct anchor doesn't).
-- target URLs are validated against a chat-host allowlist (t.me/wa.me/m.me/ig.me,
-- https only) in application code at WRITE time, so the client can render them as
-- anchor hrefs without an open-redirect / javascript:-scheme risk.
--
-- Rollback (local dev only):
--   DROP TABLE IF EXISTS tenant_widget_config;

CREATE TABLE tenant_widget_config (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
  widget_key  TEXT NOT NULL UNIQUE,
  enabled     BOOLEAN NOT NULL DEFAULT false,
  greeting    TEXT NOT NULL DEFAULT 'Chat with us',
  -- Ordered array of {kind, label, target} objects. Empty until configured.
  channels    JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (jsonb_typeof(channels) = 'array')
);

CREATE INDEX tenant_widget_config_tenant_id_idx ON tenant_widget_config (tenant_id);
-- widget_key already has a UNIQUE index (public lookup key for the bubble).

ALTER TABLE tenant_widget_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_widget_config FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_widget_config_self_all ON tenant_widget_config
  FOR ALL
  USING       (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK  (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- Role GRANTs (per the per-table grants convention, migrations 011/016/027/...).
-- Portal CRUD via polaris_anon (RLS gates rows); the public bubble config read
-- goes through polaris_service, so it needs SELECT (granted ALL for symmetry).
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_widget_config TO polaris_anon;
GRANT ALL                            ON tenant_widget_config TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('095_website_widget.sql')
ON CONFLICT (filename) DO NOTHING;
