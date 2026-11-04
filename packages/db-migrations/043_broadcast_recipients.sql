-- Migration 043 — broadcast_recipients (per-recipient state).
-- Phase 6 / PR-1. Ref: docs/Polaris V2/04g_PHASE_6_DEEP.md §3.3 +
--   05_DATA_MODEL_AND_API.md §4 (broadcast_recipients) + 00b §8.1 row 017.
-- Anchors: ADR-010 (STOP keyword pre-send check), ADR-039 (demo WABA
--   wamid semantics are tenant-scoped, not platform-global).
--
-- One row per (broadcast_id, audience_member_id) materialised at
-- POST /schedule time via the audience_filter compiler. Bulk insert
-- uses asyncpg.copy_records_to_table for ~10x throughput over batched
-- INSERT — verified GUC-honour invariant: COPY respects
-- SET LOCAL app.current_tenant_id when wrapped in a transaction, so RLS
-- enforcement holds even at the COPY fast path.
--
-- Lifecycle states (broadcast_recipient_status enum):
--   pending    — materialised; not yet picked up by worker drain
--   skipped    — compliance gate rejected pre-send; skip_reason populated.
--                reasons: 'stop_keyword'    — pre-existing STOP block (ADR-010)
--                         'opted_out'       — audience_members.opted_out_at IS NOT NULL
--                         'window_closed'   — 24h-window rule (IG/Messenger MARKETING)
--                         'cancelled'       — tenant_admin cancel transitioned this row
--                         '4xx'             — Meta returned non-retryable 4xx; failure_payload populated
--                         'retry_exhausted' — BROADCAST_RECIPIENT_RETRY_MAX hit on 5xx
--                NOTE: '4xx' and 'retry_exhausted' double as failure modes;
--                  the column stores the reason while status reflects the
--                  terminal class (skipped for compliance, failed for send).
--                  See worker.py for the exact mapping.
--   sent       — Meta accepted the send; wa_message_id (wamid) recorded.
--                wa-status webhook will progress this further.
--   delivered  — wa-status webhook status=delivered
--   read       — wa-status webhook status=read
--   failed     — wa-status webhook status=failed
--                OR Meta returned 4xx on send (no wamid)
--                OR retry budget exhausted on 5xx
--
-- wa_message_id (wamid) UNIQUE is scoped per tenant, not globally. Meta
-- wamids are astronomically unlikely to collide across tenants but the
-- semantics are documented as tenant-scoped — preserve that contract.
-- The partial UNIQUE index excludes NULLs so pre-send rows don't collide.
--
-- failure_payload captures Meta's error envelope on failure (status_code,
-- error.code, error.error_subcode, error.message). Used by the portal
-- recipient detail view to surface "why didn't this send?" without the
-- tenant_admin needing to dig into broadcast worker logs.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   DROP TABLE IF EXISTS broadcast_recipients CASCADE;
--   DROP TYPE IF EXISTS broadcast_recipient_status;

CREATE TYPE broadcast_recipient_status AS ENUM (
  'pending',
  'skipped',
  'sent',
  'delivered',
  'read',
  'failed'
);

CREATE TABLE broadcast_recipients (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  broadcast_id        UUID NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
  tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  audience_member_id  UUID NOT NULL REFERENCES audience_members(id),
  channel             TEXT NOT NULL,
  channel_user_id     TEXT NOT NULL,
  status              broadcast_recipient_status NOT NULL DEFAULT 'pending',
  wa_message_id       TEXT NULL,                                       -- wamid; UNIQUE per tenant
  variables_resolved  JSONB NOT NULL DEFAULT '{}'::jsonb,
  skip_reason         TEXT NULL,                                       -- see header for the enum-ish values
  attempt_count       INT  NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  sent_at             TIMESTAMPTZ NULL,
  delivered_at        TIMESTAMPTZ NULL,
  read_at             TIMESTAMPTZ NULL,
  failed_at           TIMESTAMPTZ NULL,
  failure_payload     JSONB NULL,                                      -- Meta error envelope on failure
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Hot-path indexes:
--   broadcast_idx — portal recipient list + worker per-broadcast progress
--   wamid_idx     — wa-status webhook resolution (wamid → recipient row);
--                   partial so pre-send NULL rows don't collide
CREATE INDEX broadcast_recipients_broadcast_idx
  ON broadcast_recipients (broadcast_id, status);
CREATE UNIQUE INDEX broadcast_recipients_wamid_idx
  ON broadcast_recipients (tenant_id, wa_message_id)
  WHERE wa_message_id IS NOT NULL;

ALTER TABLE broadcast_recipients ENABLE ROW LEVEL SECURITY;
ALTER TABLE broadcast_recipients FORCE  ROW LEVEL SECURITY;
-- Policies + grants land in 045_phase6_grants.sql. polaris_anon gets
-- SELECT + UPDATE only; INSERT happens via polaris_service inside the
-- worker for bulk COPY (the lint rule for service_pool usage is enforced
-- by apps/api/scripts/lint_service_pool.py — broadcast worker is exempt
-- from the webhook/domains constraint per 04g §9 gotchas).

INSERT INTO schema_migrations (filename) VALUES ('043_broadcast_recipients.sql')
ON CONFLICT (filename) DO NOTHING;
