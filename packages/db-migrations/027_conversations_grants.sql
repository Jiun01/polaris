-- Migration 027 — conversations grants (Phase 4 / PR-3).
-- Ref: 04e §3.2.
--
-- polaris_anon (the role every request-scoped handler uses) gets SELECT
-- + INSERT + UPDATE. UPDATE so the pipeline can bump
-- ``last_message_at`` + (in PR-7) attach a ``chatwoot_conversation_id``
-- once the handoff bridge fires. No DELETE — conversations are
-- soft-immutable from the tenant API perspective; cleanup happens via
-- the Phase 9 retention sweeper running as polaris_service.
--
-- polaris_service gets ALL because the retention sweeper + the demo-
-- session purger (ADR-038, 90d window) need DELETE.

GRANT SELECT, INSERT, UPDATE ON conversations TO polaris_anon;
GRANT ALL ON conversations TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('027_conversations_grants.sql')
ON CONFLICT (filename) DO NOTHING;
