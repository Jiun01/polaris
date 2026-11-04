-- Migration 049 — Phase 7 schema additions.
-- Phase 7 / PR-1. Ref: docs/Polaris V2/04h_PHASE_7_DEEP.md §3.1.
-- Anchors: ADR-019 (business-hours-aware handoff), ADR-024 (CW fork
--   integration), Q-PHASE7-04 (delayed private note on next inbound),
--   Q-PHASE7-06 (always PUT full Messenger Profile bundle).
--
-- Two areas touched:
--
--   (a) tenant_channels — Messenger Profile columns.
--       Stores the per-tenant Facebook Messenger entry-point config:
--       icebreakers (4 starter prompts shown above the message-input),
--       persistent_menu (hamburger menu pinned at bottom), get_started
--       (the welcome screen CTA shown to never-chatted-before customers).
--       Push state tracked via last_pushed_at + last_push_error so the
--       portal can show "out-of-sync" warnings without re-reading Meta.
--
--   (b) conversations — handoff partial-failure recovery markers.
--       When a handoff's customer-facing message succeeds but the Chatwoot
--       path fails (5xx after retries or 4xx structural), set
--       handoff_pending_recovery_at + _reason. The next inbound (in
--       webhooks/meta_router.py or webhooks/chatwoot_router.py) checks
--       this marker, posts the HANDOFF_RECOVERY_NOTE_TEMPLATE private
--       note to Chatwoot via the now-recovered API, and clears the
--       marker. Customer sees no inconsistency.
--
-- Append-only. Existing rows take JSONB defaults / NULL — no backfill.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   ALTER TABLE tenant_channels
--     DROP COLUMN IF EXISTS messenger_icebreakers,
--     DROP COLUMN IF EXISTS messenger_persistent_menu,
--     DROP COLUMN IF EXISTS messenger_get_started,
--     DROP COLUMN IF EXISTS messenger_profile_last_pushed_at,
--     DROP COLUMN IF EXISTS messenger_profile_last_push_error;
--   ALTER TABLE conversations
--     DROP COLUMN IF EXISTS handoff_pending_recovery_at,
--     DROP COLUMN IF EXISTS handoff_pending_recovery_reason;

-- ---------------------------------------------------------------------
-- (a) tenant_channels Messenger Profile columns
-- ---------------------------------------------------------------------
ALTER TABLE tenant_channels
  ADD COLUMN messenger_icebreakers             JSONB        NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN messenger_persistent_menu         JSONB        NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN messenger_get_started             JSONB        NULL,
  ADD COLUMN messenger_profile_last_pushed_at  TIMESTAMPTZ  NULL,
  ADD COLUMN messenger_profile_last_push_error TEXT         NULL;

COMMENT ON COLUMN tenant_channels.messenger_icebreakers IS
  'Array of {question, payload} per Meta /messenger_profile contract. Max 4 entries per Meta.';
COMMENT ON COLUMN tenant_channels.messenger_persistent_menu IS
  'Meta persistent_menu bundle: [{locale, composer_input_disabled, call_to_actions: [{type, title, payload|url}, ...]}, ...]. Max 3 CTAs per locale per Meta.';
COMMENT ON COLUMN tenant_channels.messenger_get_started IS
  'Meta get_started: {payload}. NULL means feature not configured by tenant.';
COMMENT ON COLUMN tenant_channels.messenger_profile_last_pushed_at IS
  'Set by services/messenger_profile.py::push_profile on successful POST to Meta. Drives portal "last pushed" display.';
COMMENT ON COLUMN tenant_channels.messenger_profile_last_push_error IS
  'NULL on success; classified error string on failure (e.g. "meta_400_invalid_payload", "meta_401_token_revoked"). Drives portal "out-of-sync" banner.';

-- ---------------------------------------------------------------------
-- (b) conversations handoff partial-failure recovery
-- ---------------------------------------------------------------------
ALTER TABLE conversations
  ADD COLUMN handoff_pending_recovery_at      TIMESTAMPTZ NULL,
  ADD COLUMN handoff_pending_recovery_reason  TEXT        NULL;

COMMENT ON COLUMN conversations.handoff_pending_recovery_at IS
  'Q-PHASE7-04: set when handoff customer-message succeeded but Chatwoot path failed. Cleared on next inbound after recovery note posted.';
COMMENT ON COLUMN conversations.handoff_pending_recovery_reason IS
  'Classified reason: chatwoot_5xx_after_retries | chatwoot_4xx | chatwoot_timeout. Surfaces in audit + admin diagnostics.';

INSERT INTO schema_migrations (filename) VALUES ('049_phase7_handoff_messenger_profile_columns.sql')
ON CONFLICT (filename) DO NOTHING;
