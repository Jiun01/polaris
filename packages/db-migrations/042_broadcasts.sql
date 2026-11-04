-- Migration 042 — broadcasts (campaign metadata).
-- Phase 6 / PR-1. Ref: docs/Polaris V2/04g_PHASE_6_DEEP.md §3.2 +
--   05_DATA_MODEL_AND_API.md §4 (broadcasts) + 00a §6.8 (outbound broadcast)
--   + 00b §8.1 row 017.
-- Anchors: ADR-026 Amendment 3rd (Pro+ tier-gate enforced in app code via
--   tenant_quotas.outbound_broadcast_enabled — NOT a DB constraint, so
--   admins can issue platform-managed broadcasts against any tenant).
--
-- Lifecycle states (broadcast_status enum):
--   draft      — created via POST /api/tenant/broadcasts; no recipients yet
--   scheduled  — POST /schedule materialised recipients + ZADD'd into Redis;
--                ready for the worker to pick up at scheduled_at
--   running    — worker has claimed (ZREM) + flipped status + populated
--                started_at; per-recipient drain in progress
--   paused     — tenant_admin paused; ZSET entry removed; resumable via
--                POST /resume which re-ZADDs
--   completed  — worker drained the recipient list cleanly; finished_at
--                populated; broadcast.completed event row written
--   cancelled  — tenant_admin cancelled; pending recipients flipped to
--                skipped (reason='cancelled'); broadcast.failed NOT
--                emitted (cancel is a tenant op, not a failure)
--   errored    — BROADCAST_DRAIN_MAX_RUNTIME_SEC exceeded OR unrecoverable
--                infrastructure failure; finished_at populated;
--                broadcast.failed event row written
--
-- audience_filter is a JSONB blob matching shared_types.broadcast.AudienceFilter
-- (discriminated union on filter_kind). Compiled at materialisation time
-- via services/audience/filter.compile_audience_filter into a parameterised
-- SQL WHERE — never string-concatenated.
--
-- variables_template stores the per-broadcast {{name}}-style template-variable
-- map. Per-recipient resolution happens at send time in worker.py — values
-- are pulled from audience_members columns (name, locale, channel_user_name)
-- and inlined into the wamid'd send call.
--
-- 5 counter columns (sent / delivered / read / failed / skipped) are
-- maintained by the worker + the wa-status webhook handler. They're
-- denormalised projections of broadcast_recipients.status counts; the
-- portal reads broadcasts directly rather than aggregating recipient rows
-- on every page load.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   DROP TABLE IF EXISTS broadcasts CASCADE;
--   DROP TYPE IF EXISTS broadcast_status;

CREATE TYPE broadcast_status AS ENUM (
  'draft',
  'scheduled',
  'running',
  'paused',
  'completed',
  'cancelled',
  'errored'
);

CREATE TABLE broadcasts (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  tenant_channel_id   UUID NOT NULL REFERENCES tenant_channels(id),
  template_id         UUID NOT NULL REFERENCES tenant_templates(id),
  name                TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  audience_filter     JSONB NOT NULL,                                   -- discriminated-union shape per shared_types.broadcast.AudienceFilter
  variables_template  JSONB NOT NULL DEFAULT '{}'::jsonb,               -- per-recipient {{name}}-style substitutions resolved at send time
  status              broadcast_status NOT NULL DEFAULT 'draft',
  scheduled_at        TIMESTAMPTZ NULL,                                  -- NULL while draft; set on schedule()
  started_at          TIMESTAMPTZ NULL,
  finished_at         TIMESTAMPTZ NULL,
  total_recipients    INT NOT NULL DEFAULT 0 CHECK (total_recipients >= 0),
  sent_count          INT NOT NULL DEFAULT 0 CHECK (sent_count >= 0),
  delivered_count     INT NOT NULL DEFAULT 0 CHECK (delivered_count >= 0),
  read_count          INT NOT NULL DEFAULT 0 CHECK (read_count >= 0),
  failed_count        INT NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
  skipped_count       INT NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
  error               JSONB NULL,                                        -- {code, message, at} for errored status
  created_by          UUID NULL,                                         -- tenant_users.id; nullable for system-issued (admin)
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Forward-progress invariants: finished_at only meaningful in terminal
  -- states; started_at only meaningful once worker has claimed.
  CHECK (finished_at IS NULL OR started_at IS NOT NULL),
  CHECK (finished_at IS NULL
         OR status IN ('completed', 'cancelled', 'errored'))
);

-- Worker-side index: drain loop reads tenant-scoped scheduled rows ordered
-- by scheduled_at to pick the head per tenant.
CREATE INDEX broadcasts_tenant_status_idx
  ON broadcasts (tenant_id, status, scheduled_at);

ALTER TABLE broadcasts ENABLE ROW LEVEL SECURITY;
ALTER TABLE broadcasts FORCE  ROW LEVEL SECURITY;
-- Policies + grants land in 045_phase6_grants.sql.

INSERT INTO schema_migrations (filename) VALUES ('042_broadcasts.sql')
ON CONFLICT (filename) DO NOTHING;
