-- Migration 075 — bookings table.
-- Phase 13 / PR-1. Ref: docs/Polaris V2/e2e/13-booking-reminders.md.
--
-- Stores one row per confirmed EA appointment that Polaris brokered.
-- The row is the Polaris-side record of the EA appointment; EA's own
-- database is the authoritative source for appointment details. Polaris
-- mirrors the minimum fields needed for:
--   * reminder scheduling (start_at, customer_handle, channel)
--   * cancel / reschedule flows (appointment_id for the EA PUT/DELETE)
--   * quota tracking (bookings_made_this_period in tenant_quotas)
--   * RLS-gated portal listing
--
-- Status lifecycle:
--   pending    — EA call in-flight or retrying after 503
--   confirmed  — EA returned 2xx; appointment_id set
--   cancelled  — customer or operator cancelled via bot / portal
--   rescheduled — customer picked a new slot; the EA appointment_id
--                 is updated in-place (EA uses PUT /appointments/{id})
--
-- Rollback (local dev only):
--   DROP TABLE IF EXISTS bookings CASCADE;
--   DROP TYPE  IF EXISTS booking_status;

CREATE TYPE booking_status AS ENUM (
  'pending',
  'confirmed',
  'cancelled',
  'rescheduled'
);

CREATE TABLE bookings (
  id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  tenant_branch_id  UUID         NULL     REFERENCES tenant_branches(id),
  conversation_id   UUID         NULL     REFERENCES conversations(id),
  appointment_id    TEXT         NULL,                    -- EA appointment ID; NULL while pending
  customer_handle   TEXT         NOT NULL,                -- WA number (+601…) or IG handle (@…)
  channel           TEXT         NOT NULL DEFAULT 'whatsapp'
                                  CHECK (channel IN ('whatsapp', 'instagram', 'messenger')),
  start_at          TIMESTAMPTZ  NOT NULL,
  end_at            TIMESTAMPTZ  NULL,
  status            booking_status NOT NULL DEFAULT 'pending',
  service_name      TEXT         NULL,
  notes             TEXT         NULL,
  created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Primary lookup paths.
CREATE INDEX bookings_tenant_idx          ON bookings (tenant_id);
CREATE INDEX bookings_tenant_start_at_idx ON bookings (tenant_id, start_at);
-- Worker + reminder scheduler scans all confirmed bookings that need a reminder.
CREATE INDEX bookings_status_start_at_idx ON bookings (status, start_at)
  WHERE status = 'confirmed';

ALTER TABLE bookings ENABLE ROW LEVEL SECURITY;
ALTER TABLE bookings FORCE  ROW LEVEL SECURITY;
-- Policies + grants land in 077_phase13_grants.sql.

INSERT INTO schema_migrations (filename) VALUES ('075_bookings.sql')
ON CONFLICT (filename) DO NOTHING;
