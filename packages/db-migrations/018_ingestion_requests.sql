-- Migration 018 — ingestion_requests (Phase 3 / PR-1).
-- Ref: docs/Polaris V2/04d_PHASE_3_DEEP.md §3.1 + 05_DATA_MODEL_AND_API.md row 012.
-- Anchors: ADR-040 (ingestion app is local-PC desktop),
--          ADR-041 (knowledge_chunks.tenant_branch_id NULL = shared across branches),
--          ADR-057 (desktop = separate project at `Polaris Ingestion System/`),
--          Q-PHASE3-07 (single desktop at GA + SKIP LOCKED defence-in-depth),
--          Q-PHASE3-10 (OCR languages default ['ch_sim','en']),
--          Q-PHASE3-11 (hybrid cron + GUI + force-run — priority column lands in 025).
--
-- Queue table. tenant_admin (polaris_anon) inserts via POST /api/tenant/knowledge/requests.
-- The desktop (Polaris Ingestion System — out of polaris monorepo per ADR-057)
-- reads via polaris_service + updates status + writes chunks_added / error.
-- SKIP LOCKED on the desktop's pop_pending() is defence-in-depth: only one
-- workstation at GA but the lock prevents accidental double-processing if a
-- second instance is ever stood up by an operator.
--
-- Per-tenant overrides for ingestion knobs (crawl_max_depth, crawl_max_pages,
-- ocr_languages, ingest_mode) live on this row so each request is self-
-- describing. chunk_size + chunk_overlap come from the API (ADR-032 Amendment
-- — desktop reads via GET /api/admin/tenants/{id}/chunking-config), NOT
-- columns here, because chunk shape is a tenant-level setting that applies
-- to all requests, not per-request.
--
-- Rollback (local only):
--   DROP TABLE IF EXISTS ingestion_requests CASCADE;
--   DROP TYPE  IF EXISTS ingestion_source_type;
--   DROP TYPE  IF EXISTS ingestion_status;
--   DROP TYPE  IF EXISTS ingestion_mode;

CREATE TYPE ingestion_source_type AS ENUM ('url', 'file', 'text');
CREATE TYPE ingestion_status AS ENUM (
  'pending', 'processing', 'partial', 'completed', 'failed', 'cancelled'
);
CREATE TYPE ingestion_mode AS ENUM ('add', 'sync', 'replace');

CREATE TABLE ingestion_requests (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  tenant_branch_id      UUID NULL REFERENCES tenant_branches(id) ON DELETE SET NULL,
  source_type           ingestion_source_type NOT NULL,
  source_url            TEXT NULL,
  storage_paths         TEXT[] NULL,
  source_text           TEXT NULL,
  source_snapshot_hash  TEXT NULL,
  ingest_mode           ingestion_mode NOT NULL DEFAULT 'sync',
  ocr_enabled           BOOLEAN NOT NULL DEFAULT false,
  ocr_languages         TEXT[] NOT NULL DEFAULT ARRAY['ch_sim','en'],   -- Q-PHASE3-10
  crawl_max_depth       INT NULL,                                       -- NULL = desktop default
  crawl_max_pages       INT NULL,
  contextual_enrichment BOOLEAN NULL,
  status                ingestion_status NOT NULL DEFAULT 'pending',
  submitted_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at            TIMESTAMPTZ NULL,
  completed_at          TIMESTAMPTZ NULL,
  chunks_added          INT NULL,
  chunks_skipped        INT NULL,
  chunks_deleted        INT NULL,
  tokens_used           INT NULL,                                       -- recorded by desktop; observed via OTel per Q-PHASE3-02
  error                 JSONB NULL,
  worker_id             TEXT NULL,                                      -- desktop INGESTION_WORKSTATION_ID
  CONSTRAINT ingestion_requests_source_shape CHECK (
        (source_type = 'url'  AND source_url IS NOT NULL AND storage_paths IS NULL AND source_text IS NULL)
     OR (source_type = 'file' AND source_url IS NULL AND storage_paths IS NOT NULL AND array_length(storage_paths, 1) > 0)
     OR (source_type = 'text' AND source_url IS NULL AND storage_paths IS NULL AND source_text IS NOT NULL AND length(source_text) <= 102400)
  )
);

-- Hot read for the desktop's pop_pending() (partial index = small + fast).
-- Migration 025 replaces this with a priority-aware variant for force-run.
CREATE INDEX ingestion_requests_status_submitted_idx
  ON ingestion_requests (status, submitted_at)
  WHERE status IN ('pending', 'processing');

-- Tenant list view ("show me my last 50 requests").
CREATE INDEX ingestion_requests_tenant_submitted_idx
  ON ingestion_requests (tenant_id, submitted_at DESC);

ALTER TABLE ingestion_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingestion_requests FORCE  ROW LEVEL SECURITY;

CREATE POLICY ingestion_requests_self_select ON ingestion_requests
  FOR SELECT
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY ingestion_requests_self_insert ON ingestion_requests
  FOR INSERT
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- UPDATE policy gates tenant-initiated cancellation. polaris_service (desktop)
-- bypasses RLS so its status transitions (pending→processing→completed/failed)
-- don't need a separate policy. No DELETE policy = append-only for tenants.
CREATE POLICY ingestion_requests_self_cancel ON ingestion_requests
  FOR UPDATE
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

INSERT INTO schema_migrations (filename) VALUES ('018_ingestion_requests.sql')
ON CONFLICT (filename) DO NOTHING;
