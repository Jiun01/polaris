-- Migration 113 — hybrid-search recall + confidence fix (QA report F4 / F2).
-- Ref: ../docs/internal-ops/PRPs/reports/bot-rag-chatflow-test-report.md findings F4, F2.
-- Anchors: migration 024 (KB search RPCs), migration 090 (product search RPCs),
--          ADR-032 (RRF k=60).
--
-- Two changes to BOTH match_knowledge_chunks_hybrid and match_products_hybrid:
--
--  F4 — BM25 leg recall. The BM25 leg filtered with
--       `content_tsv @@ websearch_to_tsquery('english', q)`. websearch_to_tsquery
--       AND-joins every content lexeme, so a chunk had to contain EVERY word in
--       the query — one extra word the chunk lacks ("get", "long", "take") drops
--       the match to zero. In testing the BM25 leg fired on only ~4 of 16 natural
--       questions; "hybrid" was de-facto vector-only. Fix: OR the query lexemes
--       (`plainto_tsquery` then swap ` & ` -> ` | `), so a chunk matching ANY
--       query word is a candidate; ts_rank still orders by how well it matches,
--       and RRF still fuses with the vector leg. Quoted-phrase support
--       (websearch-only) is dropped — acceptable for a FAQ/support bot.
--
--  F2 — confidence signal. The hybrid RPC returned only rrf_score, which is
--       rank-derived (the #1 vector hit is ALWAYS 1/(k+1) regardless of true
--       similarity), so nothing downstream could tell a strong match from the
--       nearest-of-the-irrelevant. We now also return the raw cosine distance
--       from the vector leg (`vector_distance`, lower = closer; NULL for a
--       BM25-only hit that the vector leg didn't surface). Phase: knowledge-gap
--       capture + a relevance floor consume this (migration ships the column;
--       app layer wires the threshold).
--
-- Return-type change => DROP + CREATE (CREATE OR REPLACE can't add an OUT col).
-- NOTE (F5, deferred): the text-search config stays 'english'. Switching the
-- generated content_tsv columns to a multilingual/simple config is a separate
-- maintenance-window migration (full re-tokenise + GIN reindex) and does not fix
-- Chinese (needs a segmenter extension); Chinese/Malay recall rides the vector
-- leg today.
--
-- Rollback (local only): restore the migration 024 / 090 function bodies.

-- ── KB hybrid ────────────────────────────────────────────────────────────────
DROP FUNCTION IF EXISTS match_knowledge_chunks_hybrid(TEXT, VECTOR(1024), UUID, UUID, INT, INT);

CREATE FUNCTION match_knowledge_chunks_hybrid(
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
  bm25_rank     INT,
  vector_distance REAL
)
LANGUAGE SQL
SECURITY INVOKER
SET search_path = public
AS $$
  WITH params AS (
    SELECT GREATEST(p_top_k, 1) AS top_k,
           GREATEST(p_top_k, 1) * 5 AS candidate_n,
           GREATEST(p_rrf_k, 1) AS rrf_k,
           -- F4: OR the lexemes so ANY matching term makes a chunk a candidate.
           replace(plainto_tsquery('english', p_query_text)::text, ' & ', ' | ')::tsquery AS bmq
  ),
  vec AS (
    SELECT id, content, source_name,
           (embedding <=> p_query_embedding)::real AS dist,
           row_number() OVER (ORDER BY embedding <=> p_query_embedding) AS rnk
      FROM knowledge_chunks, params
     WHERE tenant_id = p_tenant_id
       AND tenant_id = current_setting('app.current_tenant_id', true)::uuid
       AND (p_tenant_branch_id IS NULL OR tenant_branch_id IS NULL OR tenant_branch_id = p_tenant_branch_id)
     ORDER BY embedding <=> p_query_embedding
     LIMIT (SELECT candidate_n FROM params)
  ),
  bm AS (
    SELECT id, content, source_name,
           row_number() OVER (
             ORDER BY ts_rank(content_tsv, (SELECT bmq FROM params)) DESC
           ) AS rnk
      FROM knowledge_chunks, params
     WHERE tenant_id = p_tenant_id
       AND tenant_id = current_setting('app.current_tenant_id', true)::uuid
       AND (p_tenant_branch_id IS NULL OR tenant_branch_id IS NULL OR tenant_branch_id = p_tenant_branch_id)
       AND content_tsv @@ (SELECT bmq FROM params)
     ORDER BY ts_rank(content_tsv, (SELECT bmq FROM params)) DESC
     LIMIT (SELECT candidate_n FROM params)
  ),
  merged AS (
    SELECT COALESCE(v.id, b.id)                     AS id,
           COALESCE(v.content, b.content)           AS content,
           COALESCE(v.source_name, b.source_name)   AS source_name,
           v.rnk::int                               AS vector_rank,
           b.rnk::int                               AS bm25_rank,
           v.dist                                   AS vector_distance,
           (
              COALESCE(1.0 / ((SELECT rrf_k FROM params) + v.rnk), 0)
            + COALESCE(1.0 / ((SELECT rrf_k FROM params) + b.rnk), 0)
           )::real                                  AS rrf_score
      FROM vec v
      FULL OUTER JOIN bm b USING (id)
  )
  SELECT id, content, source_name, rrf_score, vector_rank, bm25_rank, vector_distance
    FROM merged
   -- Tiebreak by vector proximity (closer = better). OR-semantics (F4) makes
   -- RRF ties common when several chunks share a query keyword; without a
   -- secondary key the tie broke arbitrarily/non-deterministically and could
   -- surface a keyword-sibling over the semantically-correct chunk (report F4
   -- A1 regression). NULLS LAST keeps BM25-only hits behind vector hits on ties.
   ORDER BY rrf_score DESC, vector_distance ASC NULLS LAST
   LIMIT (SELECT top_k FROM params);
$$;

GRANT EXECUTE ON FUNCTION
  match_knowledge_chunks_hybrid(TEXT, VECTOR(1024), UUID, UUID, INT, INT)
  TO polaris_anon, polaris_service;

-- ── Product hybrid ───────────────────────────────────────────────────────────
DROP FUNCTION IF EXISTS match_products_hybrid(TEXT, VECTOR(1024), UUID, UUID, INT, INT);

CREATE FUNCTION match_products_hybrid(
  p_query_text        TEXT,
  p_query_embedding   VECTOR(1024),
  p_tenant_id         UUID,
  p_tenant_branch_id  UUID DEFAULT NULL,
  p_top_k             INT  DEFAULT 5,
  p_rrf_k             INT  DEFAULT 60
) RETURNS TABLE (
  id            UUID,
  name          TEXT,
  description   TEXT,
  price_text    TEXT,
  rrf_score     REAL,
  vector_rank   INT,
  bm25_rank     INT,
  vector_distance REAL
)
LANGUAGE SQL
SECURITY INVOKER
SET search_path = public
AS $$
  WITH params AS (
    SELECT GREATEST(p_top_k, 1) AS top_k,
           GREATEST(p_top_k, 1) * 5 AS candidate_n,
           GREATEST(p_rrf_k, 1) AS rrf_k,
           replace(plainto_tsquery('english', p_query_text)::text, ' & ', ' | ')::tsquery AS bmq
  ),
  vec AS (
    SELECT id, name, description, price_text,
           (embedding <=> p_query_embedding)::real AS dist,
           row_number() OVER (ORDER BY embedding <=> p_query_embedding) AS rnk
      FROM tenant_products, params
     WHERE tenant_id = p_tenant_id
       AND tenant_id = current_setting('app.current_tenant_id', true)::uuid
       AND is_active AND embedding IS NOT NULL
       AND (p_tenant_branch_id IS NULL OR tenant_branch_id IS NULL OR tenant_branch_id = p_tenant_branch_id)
     ORDER BY embedding <=> p_query_embedding
     LIMIT (SELECT candidate_n FROM params)
  ),
  bm AS (
    SELECT id, name, description, price_text,
           row_number() OVER (
             ORDER BY ts_rank(content_tsv, (SELECT bmq FROM params)) DESC
           ) AS rnk
      FROM tenant_products, params
     WHERE tenant_id = p_tenant_id
       AND tenant_id = current_setting('app.current_tenant_id', true)::uuid
       AND is_active
       AND (p_tenant_branch_id IS NULL OR tenant_branch_id IS NULL OR tenant_branch_id = p_tenant_branch_id)
       AND content_tsv @@ (SELECT bmq FROM params)
     ORDER BY ts_rank(content_tsv, (SELECT bmq FROM params)) DESC
     LIMIT (SELECT candidate_n FROM params)
  ),
  merged AS (
    SELECT COALESCE(v.id, b.id)                       AS id,
           COALESCE(v.name, b.name)                   AS name,
           COALESCE(v.description, b.description)     AS description,
           COALESCE(v.price_text, b.price_text)       AS price_text,
           v.rnk::int                                 AS vector_rank,
           b.rnk::int                                 AS bm25_rank,
           v.dist                                     AS vector_distance,
           (
              COALESCE(1.0 / ((SELECT rrf_k FROM params) + v.rnk), 0)
            + COALESCE(1.0 / ((SELECT rrf_k FROM params) + b.rnk), 0)
           )::real                                    AS rrf_score
      FROM vec v
      FULL OUTER JOIN bm b USING (id)
  )
  SELECT id, name, description, price_text, rrf_score, vector_rank, bm25_rank, vector_distance
    FROM merged
   -- Tiebreak by vector proximity (closer = better). OR-semantics (F4) makes
   -- RRF ties common when several chunks share a query keyword; without a
   -- secondary key the tie broke arbitrarily/non-deterministically and could
   -- surface a keyword-sibling over the semantically-correct chunk (report F4
   -- A1 regression). NULLS LAST keeps BM25-only hits behind vector hits on ties.
   ORDER BY rrf_score DESC, vector_distance ASC NULLS LAST
   LIMIT (SELECT top_k FROM params);
$$;

GRANT EXECUTE ON FUNCTION
  match_products_hybrid(TEXT, VECTOR(1024), UUID, UUID, INT, INT)
  TO polaris_anon, polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('113_hybrid_search_or_semantics_and_distance.sql')
ON CONFLICT (filename) DO NOTHING;
