-- Migration 076 — booking_reminders table.
-- Phase 13 / PR-1. Ref: docs/Polaris V2/e2e/13-booking-reminders.md.
--
-- One row per scheduled reminder per booking. The reminder worker
-- scans for rows where scheduled_at <= now() AND sent_at IS NULL
-- and sends via the booking's original channel.
--
-- A booking has exactly one reminder at GA (sent offset_hours before
-- start_at). Multiple reminders per booking (e.g. 48h + 2h) are a
-- v2.1 extension — the schema supports it via the partial index on
-- (booking_id, sent_at IS NULL) allowing multiple unsent rows per
-- booking, but the service only creates one at GA.
--
-- sent_at IS NULL = unsent (eligible for the worker to pick up).
-- sent_at IS NOT NULL = sent; worker skips (idempotency guard).
--
-- attempts / last_error give the worker retry accounting without
-- a separate retry queue. Max attempts is configured via env var
-- REMINDER_MAX_ATTEMPTS (default 3).
--
-- Rollback (local dev only):
--   DROP TABLE IF EXISTS booking_reminders CASCADE;

CREATE TABLE booking_reminders (
  id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  booking_id   UUID        NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  tenant_id    UUID        NOT NULL REFERENCES tenants(id)  ON DELETE CASCADE,
  scheduled_at TIMESTAMPTZ NOT NULL,
  sent_at      TIMESTAMPTZ NULL,       -- NULL = not yet sent
  attempts     INT         NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  last_error   TEXT        NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Worker scan: due reminders (scheduled_at <= now, not yet sent).
CREATE INDEX booking_reminders_due_idx
  ON booking_reminders (scheduled_at)
  WHERE sent_at IS NULL;

-- Lookup by booking (cancel/reschedule must void unsent reminders).
CREATE INDEX booking_reminders_booking_idx
  ON booking_reminders (booking_id)
  WHERE sent_at IS NULL;

ALTER TABLE booking_reminders ENABLE ROW LEVEL SECURITY;
ALTER TABLE booking_reminders FORCE  ROW LEVEL SECURITY;
-- Policies + grants land in 077_phase13_grants.sql.

INSERT INTO schema_migrations (filename) VALUES ('076_booking_reminders.sql')
ON CONFLICT (filename) DO NOTHING;
