-- Migration 118 — knowledge_sources desired-state ledger (reconciler redesign).
-- Ref: ../docs/internal-ops/PRPs/plans/knowledge-ingestion-reconciler-redesign.plan.md §Task 1.
-- Anchors: ADR-040 (ingestion app is local-PC desktop),
--          ADR-041 (tenant_branch_id NULL = shared across branches),
--          ADR-057 (desktop = separate project at `Polaris Ingestion System/`),
--          ADR-059 (qwen3-embedding-8b @ 1024-dim — untouchable contract),
--          Q-PHASE3-01 (embedding dim = 1024).
--
-- Purpose: Replace the *imperative* ingestion_requests queue with a
-- *declarative* knowledge_sources ledger. The portal writes one row per
-- knowledge source (desired state); the local reconciler worker diffs
-- desired_hash vs last_synced_hash and processes only what changed,
-- deleting chunks for soft-deleted sources.
--
-- This migration also:
--   * Adds knowledge_chunks.source_id FK so each chunk traces back to its
--     source row (the chunk ON DELETE CASCADE handles worker cleanup).
--   * Relaxes knowledge_chunks.request_id to NULLable — the imperative
--     queue is deprecated; existing rows are unaffected (no live data per
--     2026-05-16 wipe); future chunks come from the reconciler and have
--     source_id set instead.
--
-- Writer posture (mirrors 018/020):
--   * Tenant (polaris_anon) — INSERT via POST /api/tenant/knowledge/sources,
--     soft-DELETE via DELETE /api/tenant/knowledge/sources/{id} (UPDATE sets
--     deleted_at). SELECT via GET /api/tenant/knowledge/sources.
--   * Worker (polaris_service, BYPASSRLS) — UPDATE ledger columns
--     (last_synced_hash, status, chunk_count, error, last_synced_at) and
--     hard-DELETE tombstoned rows after chunk cleanup. No portal-facing
--     DELETE policy — tenant soft-deletes only; worker hard-deletes via BYPASSRLS.
--
-- Append-only: DO NOT edit earlier migration files.
--
-- Rollback (local dev only):
--   ALTER TABLE knowledge_chunks ALTER COLUMN request_id SET NOT NULL;
--   ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS source_id;
--   DROP TABLE IF EXISTS knowledge_sources CASCADE;

CREATE TABLE knowledge_sources (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  tenant_branch_id  UUID NULL REFERENCES tenant_branches(id) ON DELETE SET NULL,
  source_type       TEXT NOT NULL CHECK (source_type IN ('url', 'file', 'text')),
  source_url        TEXT NULL,
  storage_path      TEXT NULL,
  source_text       TEXT NULL,
  source_label      TEXT NOT NULL,
  desired_hash      TEXT NOT NULL,
  last_synced_hash  TEXT NULL,
  status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'syncing', 'synced', 'failed')),
  chunk_count       INT NOT NULL DEFAULT 0,
  error             JSONB NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_synced_at    TIMESTAMPTZ NULL,
  deleted_at        TIMESTAMPTZ NULL,
  -- Source-shape integrity: exactly one of (url|file|text) populated.
  -- Mirrors ingestion_requests_source_shape CHECK in 018:61-65.
  -- text body cap (100 KiB = 102400 bytes) also mirrors 018 + Pydantic SSOT.
  CONSTRAINT knowledge_sources_source_shape CHECK (
        (source_type = 'url'  AND source_url   IS NOT NULL AND storage_path IS NULL  AND source_text IS NULL)
     OR (source_type = 'file' AND storage_path IS NOT NULL AND source_url   IS NULL  AND source_text IS NULL)
     OR (source_type = 'text' AND source_text  IS NOT NULL AND length(source_text) <= 102400
                               AND source_url   IS NULL  AND storage_path IS NULL)
  )
);

-- Reconciler: add source_id FK to knowledge_chunks AFTER knowledge_sources
-- exists so the FK target is in place. ON DELETE CASCADE means deleting a
-- knowledge_sources row automatically cleans its chunks (the worker also
-- deletes explicitly for visibility; CASCADE is belt-and-braces).
ALTER TABLE knowledge_chunks
  ADD COLUMN source_id UUID NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE;

-- Relax request_id NOT NULL: the imperative queue is deprecated (ADR-057 +
-- reconciler redesign). New chunks from the worker set source_id instead.
-- Existing rows (if any) retain their request_id; no data migration needed
-- (knowledge_chunks was wiped 2026-05-16 per plan notes).
ALTER TABLE knowledge_chunks
  ALTER COLUMN request_id DROP NOT NULL;

-- ── Indexes ────────────────────────────────────────────────────────────────

-- Reverse lookup: "which chunks belong to this source?" — used by the
-- worker's delete-then-insert replace strategy and the ON DELETE CASCADE.
CREATE INDEX knowledge_chunks_source_id_idx
  ON knowledge_chunks (source_id);

-- Active-list read (portal GET /sources, reconciler pull desired state).
-- Partial index: only live (non-deleted) rows; deleted rows are rare and
-- accessed only by the reconciler's tombstone scan (full-table is fine there).
CREATE INDEX knowledge_sources_tenant_active_idx
  ON knowledge_sources (tenant_id)
  WHERE deleted_at IS NULL;

-- Reconciler status scan: "find all pending/syncing rows across tenants."
-- Used by the worker's classify step; low-cardinality column benefits from
-- a full index (not partial) so all status values are covered cheaply.
CREATE INDEX knowledge_sources_status_idx
  ON knowledge_sources (status);

-- ── Row-Level Security ─────────────────────────────────────────────────────
-- Mirrors 018_ingestion_requests.sql RLS posture exactly.

ALTER TABLE knowledge_sources ENABLE  ROW LEVEL SECURITY;
ALTER TABLE knowledge_sources FORCE   ROW LEVEL SECURITY;

-- Tenant reads own rows only (portal list + individual lookups).
CREATE POLICY knowledge_sources_self_select ON knowledge_sources
  FOR SELECT
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- Tenant inserts new sources (POST /api/tenant/knowledge/sources).
CREATE POLICY knowledge_sources_self_insert ON knowledge_sources
  FOR INSERT
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- Tenant soft-deletes (UPDATE deleted_at=now()) via
-- DELETE /api/tenant/knowledge/sources/{id} → UPDATE in service layer.
-- Also covers any future tenant-initiated label edits.
-- polaris_service (worker) bypasses RLS for ledger updates + hard-delete —
-- no extra policy needed (BYPASSRLS on service role).
CREATE POLICY knowledge_sources_self_update ON knowledge_sources
  FOR UPDATE
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- NO DELETE policy for tenants — soft-delete only (UPDATE deleted_at).
-- Worker hard-deletes tombstoned rows via polaris_service (BYPASSRLS).

INSERT INTO schema_migrations (filename) VALUES ('118_knowledge_sources.sql')
ON CONFLICT (filename) DO NOTHING;
