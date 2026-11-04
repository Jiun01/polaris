-- Migration 004 — tenant_channels routing rows + RLS.
-- Phase 1 / PR-1. Ref: docs/Polaris V2/04b_PHASE_1_DEEP.md §3.3 + 05_DATA_MODEL_AND_API.md §4.8.
-- Anchors: ADR-015 Amendment 3rd (Starter = 1 WhatsApp inbox; Pro+ = unlimited
--          across channel kinds. The 1-inbox cap is enforced in application
--          code against tenant_quotas.allowed_channel_kinds — NOT via a DB
--          UNIQUE constraint, because Pro+ tenants legitimately hold multiple
--          inboxes per kind).
--
-- Telegram is excluded from the `channel` CHECK constraint per the GA-trim
-- banner in docs/Polaris V2/05_DATA_MODEL_AND_API.md (telegram → v2.1).
--
-- Rollback:
--   DROP TABLE IF EXISTS tenant_channels CASCADE;

CREATE TABLE tenant_channels (
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id              UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  channel                TEXT NOT NULL CHECK (channel IN ('whatsapp', 'instagram', 'messenger')),
  chatwoot_inbox_id      INT  NOT NULL,
  waba_id                TEXT,
  phone_number_id        TEXT,
  fb_page_id             TEXT,
  ig_business_account_id TEXT,
  wa_messaging_tier      TEXT,
  mm_lite_status         TEXT,
  status                 TEXT NOT NULL DEFAULT 'active',
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE        INDEX tenant_channels_tenant_id_idx          ON tenant_channels (tenant_id);
CREATE UNIQUE INDEX tenant_channels_chatwoot_inbox_unique  ON tenant_channels (chatwoot_inbox_id);

ALTER TABLE tenant_channels ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_channels FORCE  ROW LEVEL SECURITY;

-- FOR ALL covers SELECT/INSERT/UPDATE/DELETE in one policy. WITH CHECK guards
-- INSERT/UPDATE so a row cannot be created/moved under another tenant's id.
CREATE POLICY tenant_channels_self_all ON tenant_channels
  FOR ALL
  USING       (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK  (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

INSERT INTO schema_migrations (filename) VALUES ('004_tenant_channels.sql')
ON CONFLICT (filename) DO NOTHING;
