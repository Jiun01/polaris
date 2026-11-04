-- Migration 094 — Telegram as a first-class channel (off-Meta channels M1).
-- Ref: .claude/prds/off-meta-channels.prd.md + .claude/plans/off-meta-channels.plan.md.
-- Anchors: PRD "Resolved Decisions" #3/#4/#5 — sales-led provisioning (chatwoot_inbox_id
--          stays NOT NULL, no schema loosening); per-bot webhook resolution via an
--          indexed secret column; Telegram chat_id reuses conversations identity.
--
-- This migration widens three CHECK constraints to admit 'telegram' and adds the
-- Telegram-specific routing columns to tenant_channels:
--
--   1. tenant_channels.channel  — add 'telegram' (was whatsapp/instagram/messenger,
--      migration 004; the 004 header's "telegram → v2.1" GA-trim note is now lifted).
--   2. tenant_channels columns  — telegram_bot_id (numeric bot id as TEXT),
--      telegram_bot_username (for display / deep-links), telegram_webhook_secret
--      (the per-bot resolver key; see below). All nullable — only telegram rows set them.
--   3. conversations.channel    — add 'telegram' (was whatsapp/instagram/messenger/demo,
--      migration 026). chat_id is stored as channel_user_id; the existing
--      (tenant_id, channel, channel_user_id) UNIQUE keeps one row per user<->bot.
--   4. webhook_events.source    — add 'telegram' (was the Phase-6 set in migration 046)
--      so the inbound dispatcher can record forensic rows under its own source value.
--
-- telegram_webhook_secret doubles as Telegram's setWebhook `secret_token`, returned in
-- the `X-Telegram-Bot-Api-Secret-Token` header on every delivery. Telegram restricts
-- that token to [A-Za-z0-9_-], 1-256 chars — the provisioning code MUST generate it from
-- that charset (e.g. secrets.token_urlsafe without '='), NOT raw base64/bytes. The column
-- is UNIQUE + indexed because resolve_telegram() looks the row up by it on every webhook.
-- It is a rotatable capability key, NOT the bot token (that stays encrypted in
-- tenant_secrets under kind 'telegram_bot_token').
--
-- Idempotent: drops each constraint by its auto-generated name (Postgres names an inline
-- column CHECK `<table>_<column>_check`), re-adds with the widened set; ADD COLUMN /
-- CREATE INDEX use IF NOT EXISTS. Apply order via schema_migrations.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   ALTER TABLE tenant_channels DROP CONSTRAINT tenant_channels_channel_check;
--   ALTER TABLE tenant_channels ADD  CONSTRAINT tenant_channels_channel_check
--     CHECK (channel IN ('whatsapp','instagram','messenger'));
--   ALTER TABLE tenant_channels DROP COLUMN telegram_bot_id,
--     DROP COLUMN telegram_bot_username, DROP COLUMN telegram_webhook_secret;
--   ALTER TABLE conversations DROP CONSTRAINT conversations_channel_check;
--   ALTER TABLE conversations ADD  CONSTRAINT conversations_channel_check
--     CHECK (channel IN ('whatsapp','instagram','messenger','demo'));
--   ALTER TABLE webhook_events DROP CONSTRAINT webhook_events_source_check;
--   ALTER TABLE webhook_events ADD  CONSTRAINT webhook_events_source_check
--     CHECK (source IN ('meta','chatwoot','meta-template-status','wa-status','wa-account'));

-- 1. tenant_channels.channel — admit 'telegram'.
ALTER TABLE tenant_channels
  DROP CONSTRAINT IF EXISTS tenant_channels_channel_check;

ALTER TABLE tenant_channels
  ADD CONSTRAINT tenant_channels_channel_check
  CHECK (channel IN ('whatsapp', 'instagram', 'messenger', 'telegram'));

-- 2. tenant_channels — Telegram routing columns (nullable; only telegram rows set them).
ALTER TABLE tenant_channels
  ADD COLUMN IF NOT EXISTS telegram_bot_id        TEXT,
  ADD COLUMN IF NOT EXISTS telegram_bot_username  TEXT,
  ADD COLUMN IF NOT EXISTS telegram_webhook_secret TEXT;

-- Resolver lookup key — UNIQUE so a secret maps to exactly one channel row, partial
-- so the many NULLs on non-telegram rows don't collide.
CREATE UNIQUE INDEX IF NOT EXISTS tenant_channels_telegram_webhook_secret_uq
  ON tenant_channels (telegram_webhook_secret)
  WHERE telegram_webhook_secret IS NOT NULL;

-- Defence-in-depth: the secret is used as both the URL path segment and Telegram's
-- setWebhook secret_token, which only permits [A-Za-z0-9_-], 1-256 chars. Enforce
-- that here so a malformed value (e.g. one containing '/') can never be stored and
-- then break path routing or the secret_token contract. NULL passes (non-telegram rows).
-- NB: charset is checked with an UNBOUNDED '+' and the length with char_length —
-- Postgres's regex engine caps repetition counts at 255, so a '{1,256}' bound is an
-- "invalid repetition count(s)" error (compiled lazily, so it only fires on the first
-- row INSERT, not at constraint creation).
ALTER TABLE tenant_channels
  DROP CONSTRAINT IF EXISTS tenant_channels_telegram_webhook_secret_charset;

ALTER TABLE tenant_channels
  ADD CONSTRAINT tenant_channels_telegram_webhook_secret_charset
  CHECK (
    telegram_webhook_secret ~ '^[A-Za-z0-9_-]+$'
    AND char_length(telegram_webhook_secret) BETWEEN 1 AND 256
  );

-- 3. conversations.channel — admit 'telegram'.
ALTER TABLE conversations
  DROP CONSTRAINT IF EXISTS conversations_channel_check;

ALTER TABLE conversations
  ADD CONSTRAINT conversations_channel_check
  CHECK (channel IN ('whatsapp', 'instagram', 'messenger', 'demo', 'telegram'));

-- 4. webhook_events.source — admit 'telegram'.
ALTER TABLE webhook_events
  DROP CONSTRAINT IF EXISTS webhook_events_source_check;

ALTER TABLE webhook_events
  ADD CONSTRAINT webhook_events_source_check
  CHECK (source IN (
    'meta',
    'chatwoot',
    'meta-template-status',
    'wa-status',
    'wa-account',
    'telegram'
  ));

INSERT INTO schema_migrations (filename) VALUES ('094_telegram_channel.sql')
ON CONFLICT (filename) DO NOTHING;
