-- Migration 077 — Phase 13 grants + RLS policies.
-- Phase 13 / PR-1. Ref: docs/Polaris V2/e2e/13-booking-reminders.md.
-- Pairs with 075 (bookings) and 076 (booking_reminders).
--
-- Posture:
--   bookings          — full tenant CRUD via polaris_anon under RLS.
--                       Worker (polaris_service) writes confirmed rows +
--                       flips cancelled/rescheduled via reminder flow.
--   booking_reminders — SELECT for polaris_anon (portal read-only);
--                       INSERT + UPDATE via polaris_service (reminder
--                       scheduler + worker tick).  Tenants do not create
--                       or update reminder rows directly.
--
-- RLS pattern mirrors 042/045 (broadcasts): USING + WITH CHECK both
-- gate on app.current_tenant_id GUC set by the request pool.

-- -------------------------------------------------------------------------
-- bookings
-- -------------------------------------------------------------------------
CREATE POLICY bookings_tenant_isolation ON bookings
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON bookings TO polaris_anon;
GRANT ALL ON bookings TO polaris_service;

-- -------------------------------------------------------------------------
-- booking_reminders
-- -------------------------------------------------------------------------
CREATE POLICY booking_reminders_tenant_isolation ON booking_reminders
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

GRANT SELECT ON booking_reminders TO polaris_anon;
GRANT ALL ON booking_reminders TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('077_phase13_grants.sql')
ON CONFLICT (filename) DO NOTHING;
