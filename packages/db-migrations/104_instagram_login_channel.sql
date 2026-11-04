-- Migration 104 — Instagram-Login (graph.instagram.com) as a first-class channel.
-- Ref: .claude/plans/instagram-login-channel.plan.md.
--
-- Chatwoot's "Continue with Instagram" connects the **Instagram API with Instagram
-- Login** (graph.instagram.com) — distinct from the existing 'instagram' channel,
-- which is the Messenger-API-for-Instagram (FB-page token, graph.facebook.com).
-- The two differ on BOTH the inbound signature secret (separate Instagram product
-- App Secret vs META_APP_SECRET) and the outbound endpoint/token, so they must be
-- distinguishable channel kinds rather than collapsed under 'instagram'.
--
-- This migration widens two CHECK constraints to admit 'instagram_login':
--
--   1. tenant_channels.channel  — add 'instagram_login' (was whatsapp/instagram/
--      messenger/telegram/email after migration 097).
--   2. conversations.channel    — add 'instagram_login' (was whatsapp/instagram/
--      messenger/demo/telegram after migration 094; 097 left it untouched as email
--      is Chatwoot-native with no conversations row).
--
-- No new columns: an instagram_login row reuses the existing
-- ``ig_business_account_id`` column for entry-id resolution (same as 'instagram'),
-- and its outbound token lives in tenant_secrets under the new kind
-- 'meta_ig_login_access_token' (encrypted; no schema change). No webhook_events.source
-- change — IG-Login inbound still arrives as object='instagram' (resolution kind 'ig'),
-- so forensic rows stay under source='meta'.
--
-- Idempotent: drops each constraint by its auto-generated name, re-adds with the
-- widened set. Apply order via schema_migrations.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   ALTER TABLE tenant_channels DROP CONSTRAINT tenant_channels_channel_check;
--   ALTER TABLE tenant_channels ADD  CONSTRAINT tenant_channels_channel_check
--     CHECK (channel IN ('whatsapp','instagram','messenger','telegram','email'));
--   ALTER TABLE conversations DROP CONSTRAINT conversations_channel_check;
--   ALTER TABLE conversations ADD  CONSTRAINT conversations_channel_check
--     CHECK (channel IN ('whatsapp','instagram','messenger','demo','telegram'));

-- 1. tenant_channels.channel — admit 'instagram_login'.
ALTER TABLE tenant_channels
  DROP CONSTRAINT IF EXISTS tenant_channels_channel_check;

ALTER TABLE tenant_channels
  ADD CONSTRAINT tenant_channels_channel_check
  CHECK (channel IN (
    'whatsapp', 'instagram', 'messenger', 'telegram', 'email', 'instagram_login'
  ));

-- 2. conversations.channel — admit 'instagram_login'.
ALTER TABLE conversations
  DROP CONSTRAINT IF EXISTS conversations_channel_check;

ALTER TABLE conversations
  ADD CONSTRAINT conversations_channel_check
  CHECK (channel IN (
    'whatsapp', 'instagram', 'messenger', 'demo', 'telegram', 'instagram_login'
  ));

-- 3. bookings.channel — admit 'instagram_login' so conversational/flow bookings
--    work on the new channel (parity with the 'instagram' surface; migration 075
--    set the original three-Meta-surface set, telegram was deliberately left out).
ALTER TABLE bookings
  DROP CONSTRAINT IF EXISTS bookings_channel_check;

ALTER TABLE bookings
  ADD CONSTRAINT bookings_channel_check
  CHECK (channel IN ('whatsapp', 'instagram', 'messenger', 'instagram_login'));

-- 4. Backfill allowed_channel_kinds so pro/ultra/enterprise tenants can add the
--    new instagram_login channel without an admin tier-cap override (mirrors
--    migration 099, which seeded the Meta + telegram + email kinds). Idempotent:
--    only appends when the value isn't already present. Starter stays
--    whatsapp-only by design.
UPDATE tenant_quotas
SET    allowed_channel_kinds = array_append(allowed_channel_kinds, 'instagram_login')
WHERE  plan_tier IN ('pro', 'ultra', 'enterprise')
  AND  NOT ('instagram_login' = ANY(allowed_channel_kinds));

INSERT INTO schema_migrations (filename) VALUES ('104_instagram_login_channel.sql')
ON CONFLICT (filename) DO NOTHING;
