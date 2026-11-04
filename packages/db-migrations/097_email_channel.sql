-- Migration 097 — Email as a Chatwoot-native channel (email channel MVP).
-- Ref: .claude/prds/email-channel.prd.md + .claude/plans/email-channel.plan.md.
-- Anchors: PRD Resolved Decisions #1 (Chatwoot owns transport — no Polaris IMAP/SMTP),
--          #2 (one Chatwoot Email inbox per tenant, bound via chatwoot_inbox_id, stays
--          NOT NULL — no schema loosening), #12 (discriminate email via
--          tenant_channels.channel='email'; resolve_chatwoot() already returns the
--          channel column), #15 (draft-acceptance tracking).
--
-- Unlike Telegram (094), email adds NO transport columns — Chatwoot holds the IMAP/SMTP
-- or OAuth credentials, never Polaris. This migration only:
--
--   1. tenant_channels.channel — add 'email' (was whatsapp/instagram/messenger/telegram,
--      migration 094) so resolve_chatwoot() returns channel_kind='email' for an email inbox.
--   2. email_drafts — new table backing draft-acceptance tracking (PRD RD #15). The bot's
--      suggested reply (posted as a Chatwoot private note) is stored keyed by
--      chatwoot_conversation_id; when the agent's sent reply arrives, similarity is computed
--      and the outcome recorded (accepted / edited / ignored). Service-scoped (service_pool /
--      BYPASSRLS) — written + read only by the webhook/worker path, same posture as
--      platform_mrr_snapshots (093). Add RLS + a polaris_anon grant in a follow-up only if a
--      tenant-role read surface is ever needed.
--
-- conversations.channel is intentionally NOT widened here: the email-assist MVP posts the
-- draft straight to Chatwoot off the webhook payload and does not persist a Polaris
-- conversation row. When email conversations are mirrored Polaris-side, a later migration
-- adds 'email' to conversations.channel together with the matching ConversationChannel
-- Pydantic literal (kept in lockstep to avoid DB<->type drift).
--
-- webhook_events.source is unchanged: email arrives via the Chatwoot webhook, so inbound
-- forensic rows are already recorded under source='chatwoot'.
--
-- Idempotent: constraint swap by auto-generated name; CREATE TABLE / INDEX use IF NOT EXISTS.
-- Apply order via schema_migrations.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   ALTER TABLE tenant_channels DROP CONSTRAINT tenant_channels_channel_check;
--   ALTER TABLE tenant_channels ADD  CONSTRAINT tenant_channels_channel_check
--     CHECK (channel IN ('whatsapp','instagram','messenger','telegram'));
--   DROP TABLE IF EXISTS email_drafts;

-- 1. tenant_channels.channel — admit 'email'.
ALTER TABLE tenant_channels
  DROP CONSTRAINT IF EXISTS tenant_channels_channel_check;

ALTER TABLE tenant_channels
  ADD CONSTRAINT tenant_channels_channel_check
  CHECK (channel IN ('whatsapp', 'instagram', 'messenger', 'telegram', 'email'));

-- 2. email_drafts — bot-assist draft + acceptance tracking (PRD RD #15).
CREATE TABLE IF NOT EXISTS email_drafts (
  id                        BIGSERIAL PRIMARY KEY,
  tenant_id                 UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
  channel_id                UUID REFERENCES tenant_channels (id) ON DELETE SET NULL,
  chatwoot_account_id       INTEGER NOT NULL,
  chatwoot_conversation_id  INTEGER NOT NULL,
  chatwoot_note_message_id  INTEGER,
  suggestion_text           TEXT NOT NULL,
  posted_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
  agent_reply_text          TEXT,
  similarity                REAL,
  outcome                   TEXT NOT NULL DEFAULT 'pending'
                              CHECK (outcome IN ('pending', 'accepted', 'edited', 'ignored')),
  resolved_at               TIMESTAMPTZ
);

-- Acceptance match looks up the latest pending draft for a conversation.
CREATE INDEX IF NOT EXISTS email_drafts_conv_idx
  ON email_drafts (tenant_id, chatwoot_conversation_id, posted_at DESC);

GRANT ALL ON email_drafts TO polaris_service;
GRANT USAGE, SELECT ON SEQUENCE email_drafts_id_seq TO polaris_service;
-- polaris_anon: no grant (service-path only; see header).

INSERT INTO schema_migrations (filename) VALUES ('097_email_channel.sql')
ON CONFLICT (filename) DO NOTHING;
