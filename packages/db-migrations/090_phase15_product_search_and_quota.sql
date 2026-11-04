-- Migration 090 — product search RPCs + product image quota (Phase 15 / PR-1).
-- Ref: phase-checklist/phase-15-product-catalog.md PR-1 + ADR-061 §2, §6.
-- Anchors: migration 024 (knowledge search RPCs — exact RRF template),
--          ADR-059 (embedding dim 1024 — must match tenant_products),
--          ADR-041 (tenant_branch_id NULL = shared across branches),
--          ADR-015 Amendment 3rd (channel availability per tier),
--          ADR-017 (quota dimensions are platform-managed).
--
-- Mirrors migration 024 against tenant_products. Two RPCs, both
-- SECURITY INVOKER so RLS applies as the caller (the explicit tenant_id
-- arg is belt-and-braces against a missing SET LOCAL — see 024 header).
-- Only is_active products are searchable; the bot never surfaces a paused
-- product. RPCs return product scalar fields only — the service layer
-- fetches tenant_product_images for the chosen hits separately (an RPC
-- returning image arrays would couple ranking to gallery size).
--
-- Plus: tenant_quotas.product_images_total_mb — parallel to
-- knowledge_chunks_total_mb. Tier-scaled per ADR-061 §6 (operator values
-- 2026-06-04): starter 100 MB / pro 500 MB / ultra 5 GB / enterprise custom
-- (5 GB floor, SOW override). The column DEFAULT (100 = Starter floor) is only
-- the conservative fallback for an INSERT that forgets the column; EXISTING
-- rows are backfilled by tier below, and sales-led provisioning seeds new rows
-- explicitly (same path as every other quota column — see migration 007 header).
-- NOTE: the provisioning seeder must be extended to set this column per tier
-- (tracked in phase-15 checklist PR-2).
--
-- Rollback (local only):
--   DROP FUNCTION IF EXISTS match_products_hybrid(TEXT, VECTOR, UUID, UUID, INT, INT);
--   DROP FUNCTION IF EXISTS match_products(VECTOR, UUID, UUID, INT);
--   ALTER TABLE tenant_quotas DROP COLUMN IF EXISTS product_images_total_mb;

-- ---------------------------------------------------------------
-- (1) Vector-only fallback
-- ---------------------------------------------------------------

CREATE OR REPLACE FUNCTION match_products(
  p_query_embedding   VECTOR(1024),
  p_tenant_id         UUID,
  p_tenant_branch_id  UUID DEFAULT NULL,
  p_top_k             INT  DEFAULT 5
) RETURNS TABLE (
  id           UUID,
  name         TEXT,
  description  TEXT,
  price_text   TEXT,
  distance     REAL
)
LANGUAGE SQL
SECURITY INVOKER
SET search_path = public
AS $$
  SELECT id, name, description, price_text,
         (embedding <=> p_query_embedding)::real AS distance
    FROM tenant_products
   WHERE tenant_id = p_tenant_id
     AND tenant_id = current_setting('app.current_tenant_id', true)::uuid
     AND is_active
     AND embedding IS NOT NULL
     AND (
           p_tenant_branch_id IS NULL
        OR tenant_branch_id IS NULL
        OR tenant_branch_id = p_tenant_branch_id
         )
   ORDER BY embedding <=> p_query_embedding
   LIMIT GREATEST(p_top_k, 1);
$$;

-- ---------------------------------------------------------------
-- (2) Hybrid BM25 + vector + RRF (k=60, Cormack et al. 2009)
-- ---------------------------------------------------------------

CREATE OR REPLACE FUNCTION match_products_hybrid(
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
    SELECT id, name, description, price_text,
           row_number() OVER (ORDER BY embedding <=> p_query_embedding) AS rnk
      FROM tenant_products, params
     WHERE tenant_id = p_tenant_id
       AND tenant_id = current_setting('app.current_tenant_id', true)::uuid
       AND is_active
       AND embedding IS NOT NULL
       AND (
             p_tenant_branch_id IS NULL
          OR tenant_branch_id IS NULL
          OR tenant_branch_id = p_tenant_branch_id
           )
     ORDER BY embedding <=> p_query_embedding
     LIMIT (SELECT candidate_n FROM params)
  ),
  bm AS (
    SELECT id, name, description, price_text,
           row_number() OVER (
             ORDER BY ts_rank(content_tsv,
                              websearch_to_tsquery('english', p_query_text)) DESC
           ) AS rnk
      FROM tenant_products, params
     WHERE tenant_id = p_tenant_id
       AND tenant_id = current_setting('app.current_tenant_id', true)::uuid
       AND is_active
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
    SELECT COALESCE(v.id, b.id)                       AS id,
           COALESCE(v.name, b.name)                   AS name,
           COALESCE(v.description, b.description)     AS description,
           COALESCE(v.price_text, b.price_text)       AS price_text,
           v.rnk::int                                 AS vector_rank,
           b.rnk::int                                 AS bm25_rank,
           (
              COALESCE(1.0 / ((SELECT rrf_k FROM params) + v.rnk), 0)
            + COALESCE(1.0 / ((SELECT rrf_k FROM params) + b.rnk), 0)
           )::real                                    AS rrf_score
      FROM vec v
      FULL OUTER JOIN bm b USING (id)
  )
  SELECT id, name, description, price_text, rrf_score, vector_rank, bm25_rank
    FROM merged
   ORDER BY rrf_score DESC
   LIMIT (SELECT top_k FROM params);
$$;

GRANT EXECUTE ON FUNCTION
  match_products(VECTOR(1024), UUID, UUID, INT)
  TO polaris_anon, polaris_service;

GRANT EXECUTE ON FUNCTION
  match_products_hybrid(TEXT, VECTOR(1024), UUID, UUID, INT, INT)
  TO polaris_anon, polaris_service;

-- ---------------------------------------------------------------
-- (3) Product image storage quota (parallel to knowledge_chunks_total_mb)
-- ---------------------------------------------------------------

ALTER TABLE tenant_quotas
  ADD COLUMN IF NOT EXISTS product_images_total_mb INT NOT NULL DEFAULT 100;

-- Backfill existing rows by tier (provisioning seeds new rows going forward).
UPDATE tenant_quotas q
   SET product_images_total_mb = CASE t.plan_tier
         WHEN 'starter'    THEN 100
         WHEN 'pro'        THEN 500
         WHEN 'ultra'      THEN 5120
         WHEN 'enterprise' THEN 5120   -- custom; SOW override at provisioning
         ELSE 100
       END
  FROM tenants t
 WHERE t.id = q.tenant_id;

INSERT INTO schema_migrations (filename) VALUES ('090_phase15_product_search_and_quota.sql')
ON CONFLICT (filename) DO NOTHING;
