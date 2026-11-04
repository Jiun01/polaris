-- Migration 109 — drop the legacy FB-page 'instagram' channel.
--
-- The Chatwoot-native rehaul drops the legacy Messenger-API-for-Instagram
-- (FB-page 'instagram') channel. Only 'instagram_login' (graph.instagram.com,
-- migration 104) remains as the Instagram channel. This narrows the three
-- channel CHECK constraints that migration 104 widened, removing 'instagram'.
--
-- Safe under the clean-wipe (no live tenants): there are no existing
-- 'instagram' rows. Were any present, they would need migrating to
-- 'instagram_login' (or 'messenger') first — otherwise the ADD CONSTRAINT
-- validation below would fail.
--
-- Rollback (re-admit 'instagram'):
--   ALTER TABLE tenant_channels DROP CONSTRAINT tenant_channels_channel_check;
--   ALTER TABLE tenant_channels ADD  CONSTRAINT tenant_channels_channel_check
--     CHECK (channel IN ('whatsapp','instagram','messenger','telegram','email','instagram_login'));
--   ALTER TABLE conversations DROP CONSTRAINT conversations_channel_check;
--   ALTER TABLE conversations ADD  CONSTRAINT conversations_channel_check
--     CHECK (channel IN ('whatsapp','instagram','messenger','demo','telegram','instagram_login'));
--   ALTER TABLE bookings DROP CONSTRAINT bookings_channel_check;
--   ALTER TABLE bookings ADD  CONSTRAINT bookings_channel_check
--     CHECK (channel IN ('whatsapp','instagram','messenger','instagram_login'));

-- 1. tenant_channels.channel — drop 'instagram'.
ALTER TABLE tenant_channels
  DROP CONSTRAINT IF EXISTS tenant_channels_channel_check;

ALTER TABLE tenant_channels
  ADD CONSTRAINT tenant_channels_channel_check
  CHECK (channel IN (
    'whatsapp', 'messenger', 'telegram', 'email', 'instagram_login'
  ));

-- 2. conversations.channel — drop 'instagram' (keep 'demo').
ALTER TABLE conversations
  DROP CONSTRAINT IF EXISTS conversations_channel_check;

ALTER TABLE conversations
  ADD CONSTRAINT conversations_channel_check
  CHECK (channel IN (
    'whatsapp', 'messenger', 'demo', 'telegram', 'instagram_login'
  ));

-- 3. bookings.channel — drop 'instagram'.
ALTER TABLE bookings
  DROP CONSTRAINT IF EXISTS bookings_channel_check;

ALTER TABLE bookings
  ADD CONSTRAINT bookings_channel_check
  CHECK (channel IN ('whatsapp', 'messenger', 'instagram_login'));
