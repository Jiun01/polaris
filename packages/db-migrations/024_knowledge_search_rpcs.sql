-- Migration 024 — knowledge search RPCs (Phase 3 / PR-2).
-- Ref: docs/Polaris V2/04d_PHASE_3_DEEP.md §3.7 + 05_DATA_MODEL_AND_API.md row 011.
-- Anchors: ADR-026 Amendment 5th (Cohere Rerank on Ultra+ only — these RPCs
--          feed Phase 4 RAG which then optionally reranks); ADR-031 (LLM
--          model + fallback); ADR-032 + Amendment (chunk defaults 512/80);
--          ADR-041 (tenant_branch_id NULL = shared across all branches);
--          Q-PHASE3-01 (embedding dim 1024 — must match knowledge_chunks;
--            supersedes the earlier 768 decision per cross-session sync 2026-05-17).
--
-- Two RPCs, both SECURITY INVOKER (RLS applies as the caller). The
-- explicit tenant_id arg is belt-and-braces: if a caller forgets
-- SET LOCAL app.current_tenant_id, the RLS policy on knowledge_chunks
-- strips rows silently → result set is empty. The
-- `tenant_id = current_setting(...)` predicate makes that failure
-- mode visible at the query plan level (no rows match, fail loud
-- instead of "search returns nothing for unknown reason"). Test:
-- test_search_rpcs_rls.py::test_no_set_local_returns_zero_rows.
--
-- match_knowledge_chunks_hybrid combines:
--   * vector similarity (HNSW cosine via `<=>`, lower = more similar)
--   * BM25-style ts_rank over the generated content_tsv (GIN index)
-- via Reciprocal Rank Fusion with k=60 (Cormack et al. 2009).
-- Candidate pool is top_k * 5 from each leg so the RRF merge has
-- enough overlap to score well; final cut is top_k by rrf_score.
--
-- match_knowledge_chunks is the vector-only fallback. Phase 4 calls
-- it when the query trips websearch_to_tsquery's parse rules
-- (leading `-`, emoji-only, etc.) so the search path doesn't 500.
--
-- Branch scoping per ADR-041: chunks with tenant_branch_id IS NULL
-- are shared across every branch; chunks bound to a branch only
-- surface for callers that pass that same branch (or NULL = no
-- branch filter).
--
-- Rollback (local only):
--   DROP FUNCTION IF EXISTS match_knowledge_chunks_hybrid(
--     TEXT, VECTOR, UUID, UUID, INT, INT);
--   DROP FUNCTION IF EXISTS match_knowledge_chunks(
--     VECTOR, UUID, UUID, INT);

CREATE OR REPLACE FUNCTION match_knowledge_chunks(
  p_query_embedding   VECTOR(1024),
  p_tenant_id         UUID,
  p_tenant_branch_id  UUID DEFAULT NULL,
  p_top_k             INT  DEFAULT 5
) RETURNS TABLE (
  id           UUID,
  content      TEXT,
  source_name  TEXT,
  distance     REAL
)
LANGUAGE SQL
SECURITY INVOKER
SET search_path = public
AS $$
  SELECT id, content, source_name,
         (embedding <=> p_query_embedding)::real AS distance
    FROM knowledge_chunks
   WHERE tenant_id = p_tenant_id
     AND tenant_id = current_setting('app.current_tenant_id', true)::uuid
     AND (
           p_tenant_branch_id IS NULL
        OR tenant_branch_id IS NULL
        OR tenant_branch_id = p_tenant_branch_id
         )
   ORDER BY embedding <=> p_query_embedding
   LIMIT GREATEST(p_top_k, 1);
$$;

CREATE OR REPLACE FUNCTION match_knowledge_chunks_hybrid(
  p_query_text        TEXT,
  p_query_embedding   VECTOR(1024),
  p_tenant_id         UUID,
  p_tenant_branch_id  UUID DEFAULT NULL,
  p_top_k             INT  DEFAULT 5,
  p_rrf_k             INT  DEFAULT 60
) RETURNS TABLE (
  id            UUID,
  content       TEXT,
  source_name   TEXT,
  rrf_score     REAL,
  vector_rank   INT,
  bm25_rank     INT
)
LANGUAGE SQL
SECURITY INVOKER
SET search_path = public
AS $$
  WITH params AS (
    SELECT GREATEST(p_top_k, 1) AS top_k,
           GREATEST(p_top_k, 1) * 5 AS candidate_n,
           GREATEST(p_rrf_k, 1) AS rrf_k
  ),
  vec AS (
    SELECT id, content, source_name,
           row_number() OVER (ORDER BY embedding <=> p_query_embedding) AS rnk
      FROM knowledge_chunks, params
     WHERE tenant_id = p_tenant_id
       AND tenant_id = current_setting('app.current_tenant_id', true)::uuid
       AND (
             p_tenant_branch_id IS NULL
          OR tenant_branch_id IS NULL
          OR tenant_branch_id = p_tenant_branch_id
           )
     ORDER BY embedding <=> p_query_embedding
     LIMIT (SELECT candidate_n FROM params)
  ),
  bm AS (
    SELECT id, content, source_name,
           row_number() OVER (
             ORDER BY ts_rank(content_tsv,
                              websearch_to_tsquery('english', p_query_text)) DESC
           ) AS rnk
      FROM knowledge_chunks, params
     WHERE tenant_id = p_tenant_id
       AND tenant_id = current_setting('app.current_tenant_id', true)::uuid
       AND (
             p_tenant_branch_id IS NULL
          OR tenant_branch_id IS NULL
          OR tenant_branch_id = p_tenant_branch_id
           )
       AND content_tsv @@ websearch_to_tsquery('english', p_query_text)
     ORDER BY ts_rank(content_tsv,
                      websearch_to_tsquery('english', p_query_text)) DESC
     LIMIT (SELECT candidate_n FROM params)
  ),
  merged AS (
    SELECT COALESCE(v.id, b.id)                     AS id,
           COALESCE(v.content, b.content)           AS content,
           COALESCE(v.source_name, b.source_name)   AS source_name,
           v.rnk::int                               AS vector_rank,
           b.rnk::int                               AS bm25_rank,
           (
              COALESCE(1.0 / ((SELECT rrf_k FROM params) + v.rnk), 0)
            + COALESCE(1.0 / ((SELECT rrf_k FROM params) + b.rnk), 0)
           )::real                                  AS rrf_score
      FROM vec v
      FULL OUTER JOIN bm b USING (id)
  )
  SELECT id, content, source_name, rrf_score, vector_rank, bm25_rank
    FROM merged
   ORDER BY rrf_score DESC
   LIMIT (SELECT top_k FROM params);
$$;

GRANT EXECUTE ON FUNCTION
  match_knowledge_chunks(VECTOR(1024), UUID, UUID, INT)
  TO polaris_anon, polaris_service;

GRANT EXECUTE ON FUNCTION
  match_knowledge_chunks_hybrid(TEXT, VECTOR(1024), UUID, UUID, INT, INT)
  TO polaris_anon, polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('024_knowledge_search_rpcs.sql')
ON CONFLICT (filename) DO NOTHING;
