-- Migration 021 — knowledge_chunks grants (Phase 3 / PR-1).
-- Pairs with 020_knowledge_chunks.sql.
--
-- polaris_anon: SELECT only (capacity-gate live SUM + future tenant-facing
--   chunk-browse endpoint in Phase 8 portal). No DML — chunks are immutable
--   from the tenant's perspective; "delete" happens via cancelling the parent
--   ingestion_requests row or by submitting a 'replace' mode request that
--   the desktop processes via service-role.
-- polaris_service: ALL — the desktop writes/updates/deletes chunks during
--   queue processing.
--
-- Rollback (local only):
--   REVOKE ALL ON knowledge_chunks FROM polaris_anon, polaris_service;

GRANT SELECT ON knowledge_chunks TO polaris_anon;
GRANT ALL    ON knowledge_chunks TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('021_knowledge_chunks_grants.sql')
ON CONFLICT (filename) DO NOTHING;
