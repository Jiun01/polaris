-- Migration 117 — Add ea_service_id + ea_provider_id to bookings
-- (v2/phase-13/booking-link-fixes).
--
-- The /b/{jti} redirect route needs to rebuild the EA wizard URL at click
-- time. Rather than re-resolving provider/service via the EA API on every
-- redirect (drift risk + extra latency), we store the ids at pending-booking
-- mint time and read them back here.
--
-- Both columns are nullable INT so that:
--   * Existing pending rows (minted before this migration) have NULL ids
--     and the redirect treats them as "can't rebuild" → friendly 410.
--   * The flow-runtime link-mode node (booking.py) keeps working without
--     change; it doesn't call create_pending_booking with ea ids yet and
--     its rows will NULL-gracefully 410 through the redirect.
--
-- Append-only: DO NOT edit earlier migration files.
--
-- Rollback (local dev only):
--   ALTER TABLE bookings DROP COLUMN IF EXISTS ea_service_id;
--   ALTER TABLE bookings DROP COLUMN IF EXISTS ea_provider_id;

ALTER TABLE bookings
  ADD COLUMN IF NOT EXISTS ea_service_id  INT,
  ADD COLUMN IF NOT EXISTS ea_provider_id INT;

INSERT INTO schema_migrations (filename) VALUES ('117_bookings_ea_ids.sql')
ON CONFLICT (filename) DO NOTHING;
