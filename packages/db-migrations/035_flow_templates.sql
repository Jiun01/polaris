-- Migration 035 — flow_templates (Phase 5 / PR-1).
-- Ref: docs/Polaris V2/04f_PHASE_5_DEEP.md §3.4 + 06_FLOW_BUILDER_SPEC.md §11.
-- Anchors: ADR-020 (ship 3–5 prebuilt flow templates as seeds).
--
-- ``flow_templates`` is platform-scoped, read-only by tenants. The
-- Phase 8 portal "Start from template" picker shows these on first
-- flow creation; a tenant "clones" a template by deep-copying the
-- ``definition`` JSONB into their own ``flow_definitions.draft``.
--
-- This table has NO RLS — the grant in 036 gives polaris_anon SELECT
-- only, which is the same posture as a public lookup table (tenants
-- can read but never write). polaris_service owns INSERT/UPDATE so the
-- migration 038 seed (PR-6) and any future template refresh ran via
-- the admin pool can land rows without RLS gymnastics.
--
-- Slug pattern guards against accidental SQL-string templates that
-- would never resolve in the picker UI. ``category`` mirrors the four
-- ADR-020 buckets at GA (welcome, booking, lead-capture, opt-out); a
-- CHECK constraint here would be too tight (future template
-- categories surface naturally) — the portal hard-codes the GA set in
-- its UI and treats any other category as a generic "Custom" bucket.
--
-- ``definition`` is the same JSONB shape as ``flow_versions.definition``
-- and must pass the same validator. The seed migration (038) calls the
-- validator script before inserting so a malformed template is caught
-- at migration time rather than at first tenant clone.

CREATE TABLE flow_templates (
  id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  slug             TEXT         NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9_-]+$'),
  name             TEXT         NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  description      TEXT         NOT NULL CHECK (length(description) BETWEEN 1 AND 2000),
  definition       JSONB        NOT NULL,
  category         TEXT         NOT NULL,
  created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
  last_updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
  CONSTRAINT flow_templates_definition_size_chk
    CHECK (octet_length(definition::text) <= 524288)
);

-- No ROW LEVEL SECURITY — platform-scoped lookup table. Grants in 036
-- restrict tenant role to SELECT only, which is the access posture.

INSERT INTO schema_migrations (filename) VALUES ('035_flow_templates.sql')
ON CONFLICT (filename) DO NOTHING;
