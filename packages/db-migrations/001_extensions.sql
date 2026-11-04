-- packages/db-migrations/001_extensions.sql
-- Phase 0 — first migration. Enables required extensions + bootstraps schema_migrations.
--
-- Rollback (Phase 0 only — never run post-Phase-1, see ADRs):
--   DROP TABLE IF EXISTS schema_migrations;
--   DROP EXTENSION IF EXISTS pg_trgm;
--   DROP EXTENSION IF EXISTS pgcrypto;
--   DROP EXTENSION IF EXISTS vector;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (filename) VALUES ('001_extensions.sql')
ON CONFLICT (filename) DO NOTHING;
