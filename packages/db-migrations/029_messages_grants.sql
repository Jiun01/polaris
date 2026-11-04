-- Migration 029 — messages grants (Phase 4 / PR-3).
-- Ref: 04e §3.4.
--
-- polaris_anon gets SELECT + INSERT only — messages are append-only
-- from the tenant API surface. No UPDATE (token telemetry is set at
-- INSERT time; corrections are platform work) and no DELETE (the
-- Phase 9 retention sweeper runs as polaris_service).
--
-- polaris_service gets ALL because the retention sweeper needs DELETE
-- + the DSAR export cascade may need UPDATE to anonymize content
-- columns before the conversation row itself is deleted (ADR-038).

GRANT SELECT, INSERT ON messages TO polaris_anon;
GRANT ALL ON messages TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('029_messages_grants.sql')
ON CONFLICT (filename) DO NOTHING;
