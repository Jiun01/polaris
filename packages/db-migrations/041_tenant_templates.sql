-- Migration 041 — tenant_templates (WhatsApp template lifecycle).
-- Phase 6 / PR-1. Ref: docs/Polaris V2/04g_PHASE_6_DEEP.md §3.1 +
--   05_DATA_MODEL_AND_API.md §4 (templates) + 00b_PRD_API_DATA.md §8.1 row 015.
-- Anchors: 00a §6.10 (template lifecycle), ADR-034 Amendment 3rd (paste-only
--   onboarding — submits authenticate via tenant-pasted System User token).
--
-- One row per Meta-mirrored template. Meta's uniqueness key is
-- (waba_id, name, language); we anchor instead on
-- (tenant_id, tenant_channel_id, name, language) because tenant_channel_id
-- → waba_id is a stable lookup and the FK gives ON DELETE CASCADE semantics
-- if a channel is disconnected.
--
-- meta_template_id is NULL until Meta's submit-200 callback returns it; the
-- lifecycle webhook updates the row by resolving against this column. A
-- timeout during submit leaves the row in status='pending' with
-- meta_template_id IS NULL; the recovery path (in services/templates) reads
-- GET /{waba_id}/message_templates?name=... to recover the id rather than
-- re-submitting (Meta returns 409 on duplicate name with no useful id in
-- the error envelope).
--
-- status semantics (per Meta + 00b §8.1 row 015):
--   pending   — submitted to Meta, awaiting review
--   approved  — Meta approved; sendable
--   rejected  — Meta rejected; rejection_reason populated
--   paused    — Meta paused (typically due to user-feedback signals)
--   disabled  — Meta disabled OR tenant_admin deleted (soft-disable)
--
-- name regex matches Meta's documented pattern: lowercase alphanumeric +
-- underscore, 1-512 chars. The repo's own CHECK is stricter than the API
-- accepts to catch typos client-side.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   DROP TABLE IF EXISTS tenant_templates CASCADE;

CREATE TABLE tenant_templates (
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id              UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  tenant_channel_id      UUID NOT NULL REFERENCES tenant_channels(id) ON DELETE CASCADE,
  name                   TEXT NOT NULL CHECK (name ~ '^[a-z0-9_]{1,512}$'),
  language               TEXT NOT NULL,                                   -- Meta locale code (e.g. en_US, ms_MY, zh_CN)
  category               TEXT NOT NULL CHECK (category IN (
                           'AUTHENTICATION', 'MARKETING', 'UTILITY'
                         )),
  status                 TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                           'pending', 'approved', 'rejected', 'paused', 'disabled'
                         )),
  meta_template_id       TEXT NULL,                                       -- assigned by Meta on submit-200
  components             JSONB NOT NULL,                                  -- Meta-shape components[] (header/body/footer/buttons)
  rejection_reason       TEXT NULL,
  last_status_change_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  submitted_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, tenant_channel_id, name, language)
);

-- Hot-path indexes:
--   status_idx  — portal list + status filter ("approved templates only")
--   meta_id_idx — webhook handler resolves message_template_id → row
CREATE INDEX tenant_templates_status_idx
  ON tenant_templates (tenant_id, status);
CREATE INDEX tenant_templates_meta_id_idx
  ON tenant_templates (meta_template_id)
  WHERE meta_template_id IS NOT NULL;

ALTER TABLE tenant_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_templates FORCE  ROW LEVEL SECURITY;
-- Policies + grants land in 045_phase6_grants.sql (single review surface
-- for all three new Phase 6 tables — same pattern Phase 5 used with 036).

INSERT INTO schema_migrations (filename) VALUES ('041_tenant_templates.sql')
ON CONFLICT (filename) DO NOTHING;
