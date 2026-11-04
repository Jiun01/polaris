-- Migration 073 — webhook_events_outbound Phase 7 columns.
-- Ref: services/chatwoot/handoff.py::_persist_handoff_initiated_event.
-- Anchors: Q-PHASE7-08 (handoff.initiated event row), ADR-035 Amendment.
--
-- Migration 047 created webhook_events_outbound for Phase 6 broadcast/
-- template/audience events. Phase 7's handoff.initiated event emitter
-- references two additional columns (source, external_id) that were not
-- in 047's schema:
--
--   (a) source TEXT NULL — event origin tag (e.g. 'handoff', 'broadcast').
--       Nullable for backward compat with existing rows (Phase 6 events
--       pre-date this column). Future INSERT sites should supply a value.
--
--   (b) external_id TEXT NULL — deduplication / correlation key scoped to
--       source (e.g. "<conversation_id>:<uuid_suffix>" for handoff events).
--       Nullable for backward compat. The v3.0 dispatcher may add a unique
--       index on (source, external_id) during its own migration; skipped
--       here because the handoff emitter appends a UUIDv4 suffix so
--       application-level uniqueness is already guaranteed.
--
-- Without these columns, _persist_handoff_initiated_event() raised
-- UndefinedColumnError inside its try/except, leaving the asyncpg
-- transaction in an aborted state (Python caught the exception but
-- PostgreSQL had already marked the transaction failed).
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   ALTER TABLE webhook_events_outbound
--     DROP COLUMN IF EXISTS source,
--     DROP COLUMN IF EXISTS external_id;

ALTER TABLE webhook_events_outbound
  ADD COLUMN source       TEXT NULL,
  ADD COLUMN external_id  TEXT NULL;

COMMENT ON COLUMN webhook_events_outbound.source IS
  'Event origin tag: ''handoff'', ''broadcast'', ''template'', ''audience'', ''channel''. NULL on pre-073 rows.';
COMMENT ON COLUMN webhook_events_outbound.external_id IS
  'Source-scoped deduplication key. For handoff events: "<conversation_id>:<uuid12>". NULL on pre-073 rows.';

INSERT INTO schema_migrations (filename) VALUES ('073_webhook_events_outbound_phase7_columns.sql')
ON CONFLICT (filename) DO NOTHING;
