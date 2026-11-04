-- Migration 026 — conversations (Phase 4 / PR-3).
-- Ref: docs/Polaris V2/04e_PHASE_4_DEEP.md §3.1 + 05_DATA_MODEL_AND_API.md §4.7.
-- Anchors: PRD §10 data-model, ADR-027 (demo mode), ADR-039 (demo WABA).
--
-- One row per (tenant, channel, channel_user_id). Created lazily by the
-- pipeline orchestrator (Phase 4 PR-4) when the first inbound message
-- from a fresh contact arrives; subsequent messages reuse the existing
-- row. ``chatwoot_conversation_id`` is the link to the Chatwoot
-- conversation row in the forked Chatwoot Postgres (cross-DB; no FK).
-- ``last_message_at`` is bumped by the pipeline on every save; the
-- ``(tenant_id, last_message_at DESC)`` index serves the Phase 8 portal's
-- conversation-list page.
--
-- RLS posture mirrors every other tenant-scoped table: the policy gates
-- BOTH read (USING) and write (WITH CHECK) on the
-- ``app.current_tenant_id`` GUC set per-transaction by the TenantContext
-- dep (Phase 1 PR-2). RLS is FORCED so even the service-role inherits
-- the gate when it sets the GUC — only an explicit ``SET LOCAL ROLE
-- polaris_service`` (BYPASSRLS at the role level) bypasses, which is
-- exactly what the cron / retention sweeper paths do.
--
-- The unique index ``(tenant_id, channel, channel_user_id)`` prevents
-- duplicate conversation rows from racing inbound webhooks — two
-- workers picking up the same first-touch message would both try to
-- INSERT and one would fail cleanly on the constraint instead of
-- silently forking the conversation.

CREATE TABLE conversations (
  id                            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  channel                       TEXT NOT NULL CHECK (channel IN ('whatsapp','instagram','messenger','demo')),
  channel_user_id               TEXT NOT NULL,
  chatwoot_conversation_id      INT NULL,
  last_message_at               TIMESTAMPTZ NULL,
  metadata                      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at                    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX conversations_tenant_channel_user_uq
  ON conversations (tenant_id, channel, channel_user_id);

CREATE INDEX conversations_tenant_last_message_idx
  ON conversations (tenant_id, last_message_at DESC NULLS LAST);

ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations FORCE ROW LEVEL SECURITY;

CREATE POLICY conversations_tenant_isolation ON conversations
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

INSERT INTO schema_migrations (filename) VALUES ('026_conversations.sql')
ON CONFLICT (filename) DO NOTHING;
