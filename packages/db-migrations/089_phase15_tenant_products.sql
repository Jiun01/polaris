-- Migration 089 — tenant_products + tenant_product_images (Phase 15 / PR-1).
-- Ref: phase-checklist/phase-15-product-catalog.md PR-1 + ADR-061 §1–2.
-- Anchors: ADR-042 (tenant_promotions — portal-CRUD table + RLS template),
--          ADR-059 (qwen3-embedding-8b, 1024 dim — must match knowledge_chunks),
--          ADR-041 (tenant_branch_id NULL = shared across all branches),
--          ADR-050 (8-node palette stays locked — products feed llm_reply, not a node).
--
-- Two tables shipped together:
--
--   (1) tenant_products (NEW)
--       First-class, tenant-authored catalog rows (mirrors tenant_promotions
--       posture: portal-CRUD via polaris_anon under a FOR ALL self-policy —
--       NOT the SELECT-only/service-writes posture of knowledge_chunks, which
--       is desktop-ingested). Carries an embedding (generated synchronously by
--       the API on create/update per ADR-061 §2; written through anon_pool
--       under RLS) so products are RAG-searchable via migration 090's RPCs.
--       Optional tenant_branch_id so a product can be branch-scoped (NULL =
--       tenant-wide). Optional sku for tenant-defined codes (partial-unique).
--
--   (2) tenant_product_images (NEW)
--       1..N images per product. tenant_id is DENORMALISED here (not reachable
--       via product_id alone under RLS) so the FOR ALL self-policy keys on the
--       same app.current_tenant_id GUC as every other tenant table without a
--       join. storage_path points at the private `polaris-product-images`
--       Supabase bucket ({tenant_id}/{uuid}); bytes feeds the
--       product_images_total_mb capacity gate (migration 090).
--
-- vector + pg_trgm extensions already enabled by 001_extensions.sql;
-- CREATE EXTENSION IF NOT EXISTS is a no-op safety net for partial restores.
--
-- embedding is NULLable: a row can exist before its embedding is written
-- (graceful degradation if the embed call fails mid-request). The API always
-- sets it on the same transaction in the happy path. HNSW skips NULL rows.
--
-- Rollback (local only):
--   DROP TABLE IF EXISTS tenant_product_images;
--   DROP TABLE IF EXISTS tenant_products;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------
-- (1) tenant_products
-- ---------------------------------------------------------------

CREATE TABLE tenant_products (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  tenant_branch_id  UUID NULL REFERENCES tenant_branches(id) ON DELETE SET NULL,
  sku               TEXT NULL CHECK (sku IS NULL OR length(sku) BETWEEN 1 AND 64),
  name              TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  description       TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 4000),
  price_text        TEXT NULL CHECK (price_text IS NULL OR length(price_text) BETWEEN 1 AND 100),
  is_active         BOOLEAN NOT NULL DEFAULT TRUE,
  embedding         VECTOR(1024) NULL,
  content_tsv       TSVECTOR GENERATED ALWAYS AS
                      (to_tsvector('english', name || ' ' || description)) STORED,
  metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Optional tenant-defined SKU is unique per tenant when present.
CREATE UNIQUE INDEX tenant_products_sku_unique
  ON tenant_products (tenant_id, sku)
  WHERE sku IS NOT NULL;

-- HNSW + cosine ops for vector similarity (defaults m=16, ef_construction=64,
-- per pgvector ≥0.8; matches knowledge_chunks migration 020).
CREATE INDEX tenant_products_embedding_hnsw_idx
  ON tenant_products USING hnsw (embedding vector_cosine_ops);

-- GIN over the generated tsvector for the BM25 leg of match_products_hybrid.
CREATE INDEX tenant_products_content_tsv_gin_idx
  ON tenant_products USING GIN (content_tsv);

-- Hot read: portal list-by-tenant ordered by recency.
CREATE INDEX tenant_products_tenant_idx
  ON tenant_products (tenant_id, created_at DESC);

-- Partial-active variant for the bot-side lookup (is_active filter).
CREATE INDEX tenant_products_tenant_active_idx
  ON tenant_products (tenant_id, tenant_branch_id)
  WHERE is_active;

ALTER TABLE tenant_products ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_products FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_products_self_all ON tenant_products
  FOR ALL
  USING       (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK  (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- ---------------------------------------------------------------
-- (2) tenant_product_images
-- ---------------------------------------------------------------

CREATE TABLE tenant_product_images (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  product_id    UUID NOT NULL REFERENCES tenant_products(id) ON DELETE CASCADE,
  storage_path  TEXT NOT NULL CHECK (length(storage_path) BETWEEN 1 AND 512),
  sort_order    INT  NOT NULL DEFAULT 0,
  width         INT  NULL CHECK (width  IS NULL OR width  >= 0),
  height        INT  NULL CHECK (height IS NULL OR height >= 0),
  bytes         INT  NULL CHECK (bytes  IS NULL OR bytes  >= 0),
  content_type  TEXT NULL CHECK (content_type IS NULL OR length(content_type) BETWEEN 1 AND 100),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT tenant_product_images_path_unique UNIQUE (product_id, storage_path)
);

-- Ordered fetch of a product's gallery.
CREATE INDEX tenant_product_images_product_idx
  ON tenant_product_images (product_id, sort_order);

-- Per-tenant capacity sum (SUM(bytes) for the product_images_total_mb gate).
CREATE INDEX tenant_product_images_tenant_idx
  ON tenant_product_images (tenant_id);

ALTER TABLE tenant_product_images ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_product_images FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_product_images_self_all ON tenant_product_images
  FOR ALL
  USING       (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK  (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- ---------------------------------------------------------------
-- Grants — portal-driven CRUD via polaris_anon (mirror migration 053);
-- service-role retains full access for maintenance / future admin paths.
-- ---------------------------------------------------------------

GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_products        TO polaris_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_product_images  TO polaris_anon;
GRANT ALL                            ON tenant_products        TO polaris_service;
GRANT ALL                            ON tenant_product_images  TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('089_phase15_tenant_products.sql')
ON CONFLICT (filename) DO NOTHING;
