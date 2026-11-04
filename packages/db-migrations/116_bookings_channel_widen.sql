-- Migration 116 — Widen bookings.channel CHECK to admit 'telegram' and 'email'
-- (Phase 13 / new-booking-inline Phase 1b).
--
-- Current state after migration 109_drop_legacy_instagram_channel:
--   bookings_channel_check: ('whatsapp', 'messenger', 'instagram_login')
--
-- This migration adds 'telegram' and 'email' so that link-mode new bookings
-- (create_pending_booking) can be created for customers on those channels
-- without a CHECK-constraint violation.
--
-- Email is intentionally included: email bookings are link-only (no
-- auto-confirm send), but the pending row must still insert. The confirmation
-- fires via the EA webhook → confirm_booking path.
--
-- Idempotent: DROP CONSTRAINT IF EXISTS + ADD CONSTRAINT (same pattern as
-- migrations 094, 104, 109).
--
-- Rollback (local dev only):
--   ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_channel_check;
--   ALTER TABLE bookings ADD  CONSTRAINT bookings_channel_check
--     CHECK (channel IN ('whatsapp','messenger','instagram_login'));

ALTER TABLE bookings
  DROP CONSTRAINT IF EXISTS bookings_channel_check;

ALTER TABLE bookings
  ADD CONSTRAINT bookings_channel_check
  CHECK (channel IN (
    'whatsapp',
    'messenger',
    'instagram_login',
    'telegram',
    'email'
  ));

INSERT INTO schema_migrations (filename) VALUES ('116_bookings_channel_widen.sql')
ON CONFLICT (filename) DO NOTHING;
