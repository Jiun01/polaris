-- Migration 028 — messages (Phase 4 / PR-3).
-- Ref: docs/Polaris V2/04e_PHASE_4_DEEP.md §3.3 + 05_DATA_MODEL_AND_API.md §4.6.
-- Anchors: PRD §10 data-model, ADR-031 + Amendment 2026-05-15 (model_used
--          CHECK includes 'gpt-4o-mini-fallback' baked in — supersedes the
--          old "lands as 026 + extension migration" plan since slot 026 was
--          consumed by conversations).
--
-- Polaris-side ``messages`` is the **system of record for bot decisions**
-- per PRD §10.9: intent, model used, RAG chunks, flow run, token
-- telemetry. The chatwoot.messages table mirrors chat state — sender_type,
-- conversation status, agent assignment. The two are linked via the
-- nullable ``chatwoot_message_id`` column (cross-DB; no FK).
--
-- Append-only at the tenant-anon layer (grants 029 give SELECT + INSERT
-- only; UPDATE/DELETE are platform-only via polaris_service). RLS gates
-- both read + write on the same GUC as conversations.
--
-- ``flow_run_id`` is a forward slot for Phase 5 (flow runtime) — column
-- declared NULL with NO FK constraint here; Phase 5's migration ALTERs
-- to add the FK once ``flow_runs`` exists. Per deep-dive §9.5: Phase 4
-- keeps ``flow_run_id IS NULL`` invariant (asserted by
-- test_messages_persisted in PR-4) so the Phase 5 FK migration won't
-- trip on orphan IDs.
--
-- The cache + token telemetry columns (cached_tokens, input_tokens,
-- output_tokens, cache_read_tokens) ALL land here in PR-3 even though
-- the pipeline that populates them lands in PR-4 — keeping the schema
-- complete upfront avoids a "Phase 4 lands a column then immediately
-- extends it" migration pile.
--
-- ``provider_message_id`` (the wamid / mid from Meta / Chatwoot) is the
-- inbound dedup key per Phase 2; UNIQUE partial index keeps it tenant-
-- scoped without forcing NULL-uniqueness across the whole table.
--
-- Indexes:
--   * conversation lookup: (tenant_id, conversation_id, created_at DESC)
--     drives the conversation-history loader in PR-3's
--     services/conversation.py — the hot path for every inbound message.
--   * per-tenant tail: (tenant_id, created_at DESC) for the Phase 8
--     portal's recent-activity view + the Phase 9 retention sweep.
--   * dedup: UNIQUE (tenant_id, provider_message_id) WHERE NOT NULL
--     so a webhook retry can't double-insert the same channel-side msg.

CREATE TABLE messages (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  conversation_id          UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role                     TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
  content                  TEXT NOT NULL,
  intent                   TEXT NULL CHECK (
                             intent IS NULL OR
                             intent IN ('greeting','faq','booking','handoff','promo','other')
                           ),
  token_count              INT NULL,
  model_used               TEXT NULL CHECK (
                             model_used IS NULL OR
                             model_used IN ('haiku','sonnet','gpt-4o-mini-fallback')
                           ),
  response_time_ms         INT NULL,
  cached_tokens            INT NULL,
  input_tokens             INT NULL,
  output_tokens            INT NULL,
  cache_read_tokens        INT NULL,
  rag_chunk_ids            UUID[] NULL,
  flow_run_id              UUID NULL,                 -- FK added in Phase 5 once flow_runs lands
  provider_message_id      TEXT NULL,
  chatwoot_message_id      INT NULL,                  -- cross-DB (chatwoot.messages.id); no FK
  metadata                 JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX messages_tenant_conversation_created_idx
  ON messages (tenant_id, conversation_id, created_at DESC);

CREATE INDEX messages_tenant_created_idx
  ON messages (tenant_id, created_at DESC);

CREATE UNIQUE INDEX messages_provider_message_id_uq
  ON messages (tenant_id, provider_message_id) WHERE provider_message_id IS NOT NULL;

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages FORCE ROW LEVEL SECURITY;

CREATE POLICY messages_tenant_isolation ON messages
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

INSERT INTO schema_migrations (filename) VALUES ('028_messages.sql')
ON CONFLICT (filename) DO NOTHING;
