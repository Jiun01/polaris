-- Migration 009 — audience_members (18-column shape per ADR-043).
-- Phase 1 / PR-5. Ref: docs/Polaris V2/05_DATA_MODEL_AND_API.md §4.4 + 04b §3.4.
-- Anchors: ADR-043 (full 18-column shape), Q-v1oo-final (PDPA opt-out viewer
--          is GA-blocking — opted_out_at_channel column required).
--
-- The opt-out viewer at /api/tenant/audience/opted-out (Phase 6 portal,
-- shipped at GA per the 05 trim banner) reads `opted_out_at` +
-- `opted_out_reason` + `opted_out_at_channel`. These three columns are
-- GA-blocking — without them PDPA compliance fails.
--
-- tenant_branch_id is declared as `UUID NULL` without an FK to
-- `tenant_branches` because that table lands in a later phase (Pro+
-- multi-branch — canonical migration 022 in 05 §1). The FK is added in
-- the same migration that creates `tenant_branches`; PR-5 just reserves
-- the column slot.
--
-- Partial UNIQUE on (tenant_id, channel, channel_user_id) WHERE
-- opted_out_at IS NULL enforces "one active membership per (channel
-- handle, tenant)". Re-opting-in after a STOP is supported: the old row
-- stays (opted_out_at populated, excluded from the unique index); a new
-- row is inserted for the new active membership.
--
-- Rollback (local only):
--   DROP TABLE IF EXISTS audience_members;

CREATE TABLE audience_members (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             UUID NOT NULL REFERENCES tenants(id),
  tenant_branch_id      UUID NULL,                                       -- FK added when tenant_branches lands (Pro+ multi-branch)
  channel               TEXT NOT NULL,                                   -- whatsapp|instagram_dm|messenger|telegram
  channel_user_id       TEXT NOT NULL,
  channel_user_name     TEXT NULL,
  phone_number_e164     TEXT NULL,
  email                 TEXT NULL,
  bsuid                 TEXT NULL,                                       -- Meta Business-Scoped User ID
  locale                TEXT NULL,                                       -- ms-MY | en-MY | zh-CN
  tags                  TEXT[] NOT NULL DEFAULT '{}',
  opted_in_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  opt_in_source         TEXT NOT NULL CHECK (
                          opt_in_source IN ('demo_session','csv_upload','api_sync')
                        ),
  opted_out_at          TIMESTAMPTZ NULL,
  opted_out_reason      TEXT NULL CHECK (
                          opted_out_reason IN ('stop_keyword','manual','dsar_delete')
                          OR opted_out_reason IS NULL
                        ),
  opted_out_at_channel  TEXT NULL,                                       -- channel handle/inbox the STOP arrived on (PDPA-required)
  chatwoot_contact_id   INT NULL,
  metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX uniq_audience_active
  ON audience_members (tenant_id, channel, channel_user_id)
  WHERE opted_out_at IS NULL;

CREATE INDEX audience_members_tenant_optout_idx
  ON audience_members (tenant_id, opted_out_at)
  WHERE opted_out_at IS NOT NULL;
-- Powers the GA-blocking opt-out viewer.

ALTER TABLE audience_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE audience_members FORCE  ROW LEVEL SECURITY;

CREATE POLICY audience_members_self_all ON audience_members
  FOR ALL
  USING       (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK  (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

INSERT INTO schema_migrations (filename) VALUES ('009_audience_members.sql')
ON CONFLICT (filename) DO NOTHING;
