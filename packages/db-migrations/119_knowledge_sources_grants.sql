-- Migration 119 — knowledge_sources grants (reconciler redesign).
-- Pairs with 118_knowledge_sources.sql — the companion grants migration,
-- mirroring the 020_knowledge_chunks → 021_knowledge_chunks_grants split.
--
-- WHY THIS EXISTS / the bug it fixes:
--   Postgres RLS policies are evaluated ON TOP OF table-level privileges.
--   118 shipped the table + ENABLE/FORCE RLS + self_select/insert/update
--   policies, but WITHOUT any GRANT. Result: polaris_anon (the role the API
--   uses for tenant requests) has no table privilege at all and every query
--   fails with "permission denied for table knowledge_sources" — which the
--   portal surfaces as "We're having trouble loading your knowledge sources."
--   The policies were moot without these grants.
--
-- Privilege posture (mirrors 021_knowledge_chunks_grants.sql):
--   polaris_anon  — SELECT (GET /api/tenant/knowledge/sources list),
--                   INSERT (POST /sources), UPDATE (soft-delete sets
--                   deleted_at; also future tenant label edits).
--                   NO DELETE — tenants soft-delete only; the worker
--                   hard-deletes tombstoned rows.
--   polaris_service — ALL: the local reconciler worker (BYPASSRLS) reads
--                   desired state, updates ledger columns
--                   (last_synced_hash, status, chunk_count, error,
--                   last_synced_at), and hard-deletes tombstoned rows.
--
-- knowledge_chunks grants are unchanged: 021 already grants the whole table
-- to both roles, which covers the new source_id column added in 118 (table
-- GRANTs are not column-scoped here).
--
-- Append-only: DO NOT edit 118 (immutable post-merge). This is a new file.
--
-- Rollback (local dev only):
--   REVOKE ALL ON knowledge_sources FROM polaris_anon, polaris_service;

GRANT SELECT, INSERT, UPDATE ON knowledge_sources TO polaris_anon;
GRANT ALL                    ON knowledge_sources TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('119_knowledge_sources_grants.sql')
ON CONFLICT (filename) DO NOTHING;
