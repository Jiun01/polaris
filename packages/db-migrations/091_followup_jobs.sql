-- Migration 091 — followup_jobs table + RLS + grants.
-- Phase 13 / follow-ups PR-1. Ref: AI re-engagement follow-up feature.
--
-- One row per pending AI follow-up for a conversation. Mirrors the
-- booking_reminders worker pattern (076): the follow-up worker scans for
-- rows where scheduled_at <= now() AND sent_at IS NULL and sends a
-- context-aware re-engagement message inside WhatsApp's free 24h window.
--
-- Lifecycle:
--   * Scheduled in the inbound pipeline (anon pool, RLS) after a
--     qualifying bot reply, at a quiet-hours-aware "smart" time.
--   * Cancelled (DELETED) on the next inbound or on handoff — the
--     customer is active again, so the nudge is no longer warranted.
--   * Claimed + sent by the follow-up worker (service pool, BYPASSRLS,
--     cross-tenant scan), which re-verifies eligibility at send time.
--
-- anchor_inbound_at = the customer's last inbound timestamp the schedule
-- was computed from. The worker skips (supersede guard) if a newer
-- role='user' message exists at send time — defends the cancel-on-inbound
-- race.
--
-- sent_at IS NULL = pending (worker-eligible). sent_at IS NOT NULL =
-- terminal (sent OR window_closed); worker skips (idempotency guard).
--
-- At most ONE pending follow-up per conversation (unique partial index) —
-- "one follow-up per silence episode". A new episode (next inbound →
-- reply) schedules a fresh row via ON CONFLICT upsert.
--
-- attempts / last_error give the worker retry accounting without a
-- separate queue (cap via env FOLLOWUP_MAX_ATTEMPTS, default 3).
--
-- Rollback (local dev only):
--   DROP TABLE IF EXISTS followup_jobs CASCADE;

CREATE TABLE followup_jobs (
  id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID        NOT NULL REFERENCES tenants(id)       ON DELETE CASCADE,
  conversation_id   UUID        NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  anchor_inbound_at TIMESTAMPTZ NOT NULL,
  scheduled_at      TIMESTAMPTZ NOT NULL,
  sent_at           TIMESTAMPTZ NULL,       -- NULL = pending
  attempts          INT         NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  last_error        TEXT        NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Worker scan: due follow-ups (scheduled_at <= now, not yet sent).
CREATE INDEX followup_jobs_due_idx
  ON followup_jobs (scheduled_at)
  WHERE sent_at IS NULL;

-- One pending follow-up per conversation (upsert target for the scheduler).
CREATE UNIQUE INDEX followup_jobs_one_pending_per_conversation
  ON followup_jobs (conversation_id)
  WHERE sent_at IS NULL;

ALTER TABLE followup_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE followup_jobs FORCE  ROW LEVEL SECURITY;

-- RLS mirrors 077 (booking_reminders): USING + WITH CHECK gate on the
-- app.current_tenant_id GUC the request/anon pool sets.
CREATE POLICY followup_jobs_tenant_isolation ON followup_jobs
  USING       (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK  (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- Anon (request pool) schedules + cancels inside the inbound pipeline, so
-- it needs INSERT/UPDATE/DELETE (unlike booking_reminders, whose scheduler
-- runs on the service pool). Service (worker) gets ALL for claim + mark.
GRANT SELECT, INSERT, UPDATE, DELETE ON followup_jobs TO polaris_anon;
GRANT ALL ON followup_jobs TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('091_followup_jobs.sql')
ON CONFLICT (filename) DO NOTHING;
