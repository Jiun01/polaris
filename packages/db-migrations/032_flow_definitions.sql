-- Migration 032 — flow_definitions (Phase 5 / PR-1).
-- Ref: docs/Polaris V2/04f_PHASE_5_DEEP.md §3.1 + 06_FLOW_BUILDER_SPEC.md §8 + 05_DATA_MODEL_AND_API.md (flow runtime).
-- Anchors: ADR-007 (hex runtime), ADR-050 (8-node GA palette, draft+published only).
--
-- ``flow_definitions`` is one row per (tenant, flow_name). It holds:
--   * the editable working draft (``draft`` JSONB) — mutated in place
--     by the Phase 8 portal canvas via ``POST /api/tenant/flows``;
--   * a pointer at the currently-published version (``current_version_id``),
--     which is NULL until the first successful publish.
--
-- The FK on ``current_version_id → flow_versions(id)`` is added in
-- migration 033 (after ``flow_versions`` exists) and is DEFERRABLE
-- INITIALLY DEFERRED so the publish path can write a new
-- ``flow_versions`` row and flip ``current_version_id`` in one
-- transaction without ordering constraints.
--
-- RLS posture mirrors every other tenant-scoped Phase 4 table
-- (conversations, messages): policy + grants land in
-- 036_flow_runtime_grants.sql so a single review surface covers the
-- four-table runtime introduction. ``ENABLE`` + ``FORCE`` here keeps
-- the table unusable until the policy is created — RLS-without-policy
-- denies all rows by default.
--
-- ``draft`` defaults to an empty, validator-clean shape so a fresh
-- flow row is immediately drag-and-drop-ready in the portal canvas.
-- The 512 KB cap on draft size is enforced at publish time
-- (``flow_versions`` CHECK), not on the draft itself — partially-built
-- definitions in the editor can temporarily exceed the cap before the
-- tenant trims them. Worst case the draft can hold up to the row-size
-- bound (~1 GB) before TOAST kicks in.
--
-- ``archived_at`` is the soft-delete column. The
-- ``flow_definitions_tenant_idx`` partial index skips archived rows
-- so the portal's flow-list query stays O(active flows) instead of
-- O(history).

CREATE TABLE flow_definitions (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name                TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  description         TEXT NULL CHECK (description IS NULL OR length(description) <= 2000),
  draft               JSONB NOT NULL DEFAULT '{"nodes":[],"edges":[],"variables":{},"version":"1.0"}'::jsonb,
  current_version_id  UUID NULL,                       -- FK added in migration 033 (DEFERRABLE)
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  archived_at         TIMESTAMPTZ NULL,
  CONSTRAINT flow_definitions_tenant_name_uq UNIQUE (tenant_id, name)
);

CREATE INDEX flow_definitions_tenant_idx
  ON flow_definitions (tenant_id, updated_at DESC)
  WHERE archived_at IS NULL;

ALTER TABLE flow_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE flow_definitions FORCE ROW LEVEL SECURITY;

-- Policy lives in 036_flow_runtime_grants.sql so the four runtime
-- tables can share one review surface.

INSERT INTO schema_migrations (filename) VALUES ('032_flow_definitions.sql')
ON CONFLICT (filename) DO NOTHING;
