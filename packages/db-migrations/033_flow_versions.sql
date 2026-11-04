-- Migration 033 — flow_versions (Phase 5 / PR-1).
-- Ref: docs/Polaris V2/04f_PHASE_5_DEEP.md §3.2 + 06_FLOW_BUILDER_SPEC.md §8.
-- Anchors: ADR-007 (immutable published snapshots), ADR-050 (draft+published only at GA;
--          parent_version_id stays NULL at GA — version history rollback ships post-GA).
--
-- ``flow_versions`` is the append-only published-snapshot table. Once a
-- row is written, it must never mutate — the entire publish/rollback
-- model relies on existing version rows staying byte-identical so
-- ``flow_definitions.current_version_id`` can flip back to a prior row
-- and resurrect that exact graph.
--
-- Immutability is enforced two ways:
--   1. Missing GRANT — migration 036 gives polaris_anon SELECT + INSERT
--      only; UPDATE/DELETE return permission-denied at the row level.
--   2. CHECK constraint on ``definition`` size — a 512 KB cap matches
--      the runtime env var (FLOW_RUNTIME_DEFINITION_MAX_KB) so a row
--      that survives publish is guaranteed to fit the runtime's
--      validator/loader budget.
--
-- ``tenant_id`` is denormalised from flow_definitions because RLS
-- needs to gate this table without a join — single-column predicate on
-- ``app.current_tenant_id`` keeps the policy cheap and matches every
-- other tenant-scoped table in the schema.
--
-- ``parent_version_id`` is a self-FK that captures the rollback chain
-- (and post-GA, the "forked from version X" relationship). At GA per
-- ADR-050 there is no UI surface for it — we set it on every insert as
-- a forward-compat slot so v2.1+'s rollback history view has the data
-- already.
--
-- ``published_by`` is nullable so the migration 038 seed (4 starter
-- templates) can insert system rows without a user_id. Tenant publishes
-- always populate it from the portal JWT subject claim. The column
-- intentionally has no FK to a users table — Phase 8 hasn't decided on
-- the portal user model yet; we'll back-fill the FK when that lands.
--
-- The (flow_definition_id, published_at DESC) index serves three
-- queries: the rollback "list prior versions" dropdown (Phase 8),
-- the publish-time "what's the latest version_id for this flow"
-- lookup (Phase 5 runner), and the v2.1 version-diff viewer.
--
-- The DEFERRABLE FK from flow_definitions.current_version_id is added
-- LAST in this migration so the publish path can:
--   BEGIN;
--   INSERT INTO flow_versions (...) RETURNING id INTO _new_id;
--   UPDATE flow_definitions SET current_version_id = _new_id WHERE id = _flow_id;
--   COMMIT;
-- Without DEFERRABLE, the FK check fires on the UPDATE and races with
-- the INSERT's row-visibility. With it, the check runs at COMMIT once
-- both rows exist.

CREATE TABLE flow_versions (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  flow_definition_id  UUID NOT NULL REFERENCES flow_definitions(id) ON DELETE CASCADE,
  tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  definition          JSONB NOT NULL,
  published_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_by        UUID NULL,
  parent_version_id   UUID NULL REFERENCES flow_versions(id),
  CONSTRAINT flow_versions_definition_size_chk
    CHECK (octet_length(definition::text) <= 524288)   -- 512 KB
);

CREATE INDEX flow_versions_flow_published_idx
  ON flow_versions (flow_definition_id, published_at DESC);

CREATE INDEX flow_versions_tenant_idx
  ON flow_versions (tenant_id, published_at DESC);

ALTER TABLE flow_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE flow_versions FORCE ROW LEVEL SECURITY;

-- Add the back-FK from flow_definitions.current_version_id now that
-- the target table exists. DEFERRABLE INITIALLY DEFERRED so the publish
-- path can insert+update inside one transaction.
ALTER TABLE flow_definitions
  ADD CONSTRAINT flow_definitions_current_version_fk
  FOREIGN KEY (current_version_id) REFERENCES flow_versions(id)
  DEFERRABLE INITIALLY DEFERRED;

INSERT INTO schema_migrations (filename) VALUES ('033_flow_versions.sql')
ON CONFLICT (filename) DO NOTHING;
