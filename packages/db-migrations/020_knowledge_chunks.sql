-- Migration 020 — knowledge_chunks (Phase 3 / PR-1).
-- Ref: docs/Polaris V2/04d_PHASE_3_DEEP.md §3.3 + 05_DATA_MODEL_AND_API.md row 011.
-- Anchors: ADR-032 + Amendment (KB defaults chunk_size=512, overlap=80),
--          ADR-041 (tenant_branch_id NULL = shared across all branches),
--          Q-PHASE3-01 (embedding dim = 1024; supersedes the earlier 768
--            decision after a cross-session coordination check found the
--            desktop project had moved to a 1024-dim model — see
--            BUILD_GUIDE.md for the current model pin),
--          Q-PHASE3-15 (chunk config stored in tenants.settings JSONB),
--          Q-PHASE3-17 (capacity gate uses live SUM(octet_length(content))).
--
-- The highest-value cross-tenant-leak target in the system. RLS posture is
-- tight: SELECT-only policy keyed on the same `app.current_tenant_id` GUC
-- that gates every other tenant table. No INSERT/UPDATE/DELETE policy →
-- polaris_anon cannot write. Only polaris_service (BYPASSRLS, desktop's
-- service-role pool) writes; the desktop never holds a TenantContext because
-- it operates per-request out-of-band of any tenant's HTTP session.
--
-- Dim 1024 locked per Q-PHASE3-01 (superseded 2026-05-17): Qwen3-Embedding
-- (Qwen3-Embedding-0.6B is 1024 native; larger variants 4B/8B can be
-- MRL-truncated to 1024). Specific variant pin lives in
-- `Polaris Ingestion System/BUILD_GUIDE.md §2` so the model can be
-- swapped without touching SQL as long as the dim stays at 1024.
-- Past tenant data was deleted (2026-05-16) so the dim change has zero
-- re-embed cost vs the prior 768 spec on this branch (which was never
-- pushed to Supabase).
--
-- vector_cosine_ops requires L2-normalised vectors to be semantically
-- equivalent to (1 - dot product). Qwen3-Embedding does NOT normalise
-- at the raw HF-inference level — the `sentence-transformers` wrapper
-- normalises (`normalize_embeddings=True`), so the desktop must either
-- use the wrapper OR normalise explicitly before insert. Silently
-- un-normalised vectors won't error here — they'll just degrade search
-- quality in a way that looks like a relevance regression, not a bug.
--
-- HNSW with default opts (m=16, ef_construction=64). pgvector docs suggest
-- these are appropriate for up to ~1M vectors per index; revisit at Phase 10
-- perf gate if recall/latency drift on a tenant with > 500k chunks.
--
-- The vector + pg_trgm extensions are already enabled by 001_extensions.sql;
-- CREATE EXTENSION IF NOT EXISTS is a no-op + safety net for partial restores.
--
-- Rollback (local only):
--   DROP TABLE IF EXISTS knowledge_chunks CASCADE;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE knowledge_chunks (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  tenant_branch_id  UUID NULL REFERENCES tenant_branches(id) ON DELETE SET NULL,
  request_id        UUID NOT NULL REFERENCES ingestion_requests(id) ON DELETE CASCADE,
  source_name       TEXT NOT NULL,
  source_type       TEXT NOT NULL CHECK (source_type IN ('web', 'document', 'text', 'faq')),
  source_refs       JSONB NOT NULL DEFAULT '[]'::jsonb,
  chunk_index       INT NOT NULL,
  content           TEXT NOT NULL,
  content_hash      TEXT NOT NULL,
  content_tsv       TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
  embedding         VECTOR(1024) NOT NULL,
  token_count       INT NOT NULL,
  source_metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT knowledge_chunks_per_tenant_hash_unique UNIQUE (tenant_id, content_hash)
);

-- HNSW + cosine ops for vector similarity. Defaults (m=16, ef_construction=64)
-- per pgvector ≥0.8 recommendations for general use.
CREATE INDEX knowledge_chunks_embedding_hnsw_idx
  ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);

-- GIN over the generated tsvector column for BM25-style search in the hybrid RPC.
CREATE INDEX knowledge_chunks_content_tsv_gin_idx
  ON knowledge_chunks USING GIN (content_tsv);

-- Tenant + branch composite — supports ADR-041 branch-scoped filtering when
-- the hybrid RPC narrows by branch.
CREATE INDEX knowledge_chunks_tenant_branch_idx
  ON knowledge_chunks (tenant_id, tenant_branch_id);

-- Per-request reverse lookup ("delete all chunks for request X" — ON DELETE
-- CASCADE handles it automatically, but the index also helps tenant queries
-- like "how many chunks did request X produce?").
CREATE INDEX knowledge_chunks_request_idx
  ON knowledge_chunks (request_id);

ALTER TABLE knowledge_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_chunks FORCE  ROW LEVEL SECURITY;

CREATE POLICY knowledge_chunks_self_select ON knowledge_chunks
  FOR SELECT
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- No INSERT/UPDATE/DELETE policy: polaris_anon never writes chunks. Only
-- polaris_service writes (the desktop's service-role pool, via BYPASSRLS).

INSERT INTO schema_migrations (filename) VALUES ('020_knowledge_chunks.sql')
ON CONFLICT (filename) DO NOTHING;
