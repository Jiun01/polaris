-- Migration 110 — messages.metadata column-level UPDATE grant.
-- Ref: prod 2026-06-13 (Chatwoot-native cutover) — dispatch-failure stamp warning
--      "pipeline: failed to stamp dispatch failure on msg=… err=InsufficientPrivilegeError".
--
-- Migration 029 grants polaris_anon SELECT + INSERT on messages (append-only at the
-- anon layer); migration 031 adds a column-scoped UPDATE on provider_message_id
-- (the post-dispatch wamid patch). The Phase-4 pipeline's dispatch-failure recorder
-- (_record_dispatch_failure in apps/api/polaris_api/services/pipeline.py) additionally
-- does, via the anon pool under RLS:
--
--   UPDATE messages
--      SET metadata = metadata || jsonb_build_object('dispatch', jsonb_build_object(...))
--    WHERE id = $... AND tenant_id = $...
--
-- when a provider send returns non-2xx (e.g. the Messenger 400 observed at cutover).
-- That UPDATE touches the `metadata` column, for which polaris_anon has no grant, so
-- it raises InsufficientPrivilegeError (SQLSTATE 42501 — a privilege error, NOT an RLS
-- row-denial). The stamp is best-effort so dispatch itself is unaffected, but the
-- per-message failure trail + dispatch-failure correlation are silently lost.
--
-- Column-level GRANT preserves the append-only intent: only provider_message_id (031)
-- and now metadata may be patched after INSERT. content / intent / model_used / tokens
-- etc. stay immutable from the anon layer. polaris_service is unchanged (full UPDATE
-- via migration 029). GRANT is idempotent, so re-running is a harmless no-op.
--
-- Rollback (local only):
--   REVOKE UPDATE (metadata) ON messages FROM polaris_anon;

GRANT UPDATE (metadata) ON messages TO polaris_anon;

INSERT INTO schema_migrations (filename) VALUES ('110_messages_metadata_grant.sql')
ON CONFLICT (filename) DO NOTHING;
