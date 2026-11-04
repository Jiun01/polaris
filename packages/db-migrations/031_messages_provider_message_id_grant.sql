-- Migration 031 — messages.provider_message_id column-level UPDATE grant (Phase 4 smoke fix).
-- Ref: smoke run 2026-05-18 + .claude/reviews/pr-25-review.md follow-up.
--
-- Migration 029 grants polaris_anon SELECT + INSERT on messages only
-- (append-only at the anon layer). But the Phase 4 pipeline does:
--
--   1. INSERT messages (assistant) with provider_message_id=NULL
--   2. dispatch via meta_client → returns wamid
--   3. UPDATE messages SET provider_message_id = wamid WHERE id = ...
--
-- Step 3 fails with InsufficientPrivilegeError because polaris_anon
-- has no UPDATE grant. The pipeline logs a warning and continues, but
-- messages.provider_message_id stays NULL — breaks Phase 6+ webhook
-- correlation (wamid → messages row lookup).
--
-- Column-level GRANT preserves the broader append-only intent: only
-- this one column can be patched after INSERT. Everything else
-- (content, intent, model_used, tokens, etc.) stays immutable from
-- the anon layer. polaris_service retains full UPDATE/DELETE for the
-- Phase 9 retention sweeper (no change).
--
-- Rollback (local only):
--   REVOKE UPDATE (provider_message_id) ON messages FROM polaris_anon;

GRANT UPDATE (provider_message_id) ON messages TO polaris_anon;

INSERT INTO schema_migrations (filename) VALUES ('031_messages_provider_message_id_grant.sql')
ON CONFLICT (filename) DO NOTHING;
