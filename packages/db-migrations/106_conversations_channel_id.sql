-- Migration 106 — conversations.channel_id (inbound WABA pinning).
-- Ref: outbound-dispatch correctness fixes; ADR-015 (tenant_channels
-- multiplicity — a tenant may own N rows of the same channel kind).
--
-- conversations (migration 026) recorded only ``channel`` (the kind) and
-- ``channel_user_id`` — NOT which specific tenant_channels row the inbound
-- message arrived on. For a multi-WABA WhatsApp tenant that lost the link
-- between a conversation and the exact business number the customer messaged,
-- so:
--
--   * services/pipeline.py::_resolve_whatsapp_credentials picked an ARBITRARY
--     'whatsapp' row (no ORDER/LIMIT), and
--   * services/webhook_resolver.py::resolve_agent_forward_context picked the
--     OLDEST row (ORDER BY created_at ASC LIMIT 1)
--
-- — either of which could reply from the WRONG WABA number (or one outside the
-- customer's 24h window, which Meta then rejects). The pipeline path now threads
-- the inbound IncomingMessage.channel_id directly; the agent-forward path has no
-- inbound message in hand, so it reads the pinned id off the conversation.
--
-- This column records that inbound tenant_channels.id at conversation creation
-- (services/pipeline.py::_ensure_conversation). Nullable + ON DELETE SET NULL:
-- conversations created before this migration stay NULL and the resolver falls
-- back to its prior ordering (correct for the single-WABA common case); a
-- deleted channel row nulls the link rather than cascading the conversation.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   ALTER TABLE conversations DROP COLUMN IF EXISTS channel_id;

ALTER TABLE conversations
  ADD COLUMN channel_id UUID NULL REFERENCES tenant_channels(id) ON DELETE SET NULL;

COMMENT ON COLUMN conversations.channel_id IS
  'The specific tenant_channels.id the inbound message arrived on (ADR-015 multiplicity). Set at creation by services/pipeline.py::_ensure_conversation. Pins outbound replies (bot + human-agent forward) to the SAME WABA/page the customer messaged. NULL for rows created before migration 106 — callers fall back to channel-kind ordering.';

INSERT INTO schema_migrations (filename) VALUES ('106_conversations_channel_id.sql')
ON CONFLICT (filename) DO NOTHING;
