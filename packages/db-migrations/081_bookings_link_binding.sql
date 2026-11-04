-- Migration 081 — booking-via-EA-link HMAC binding (v2.1 / PR-1).
-- Ref: docs/Polaris V2/09_DECISION_LOG.md (EA-link binding-token ADR).
--
-- The booking node is moving from "Polaris calls EA create_appointment
-- directly" to "Polaris sends the customer an EA web link; EA's own UI
-- handles slot selection". A booking made on EA's web page has no idea
-- who the chat user is, so we carry the verified channel identity across
-- the detour with a server-minted HMAC token:
--
--   1. Booking node mints a pending bookings row with
--      customer_handle = conversations.channel_user_id (the VERIFIED,
--      unspoofable phone / IGSID / PSID) and a signed binding_token.
--   2. The EA link embeds the token as ?polaris_ref=<token>.
--   3. EA stores it as a hidden field and fires it back via the EA
--      inbound webhook (POST /v1/webhooks/ea).
--   4. The webhook resolves the pending booking by binding_token and
--      flips it to confirmed with the EA appointment_id.
--
-- The token is the bridge; identity is locked at mint time, never typed
-- by the customer. Same mechanism for WhatsApp / Instagram / Messenger.
--
-- binding_token here stores the token's lookup id (the HMAC payload's
-- jti). The full signed token lives only in the URL; the webhook
-- verifies the signature, then looks the row up by this id. A UNIQUE
-- partial index gives O(1) webhook resolution and rejects duplicates.
--
-- bookings_handle_lookup_idx backs the cancel/reschedule path: look up
-- a customer's confirmed upcoming bookings by their verified handle so
-- Customer A can never reach Customer B's booking (no typed identity to
-- spoof — the lookup key is the live webhook sender id).
--
-- Grants: bookings already has table-level grants (migration 077); new
-- columns inherit them. No GRANT changes here.
--
-- Rollback (local dev only):
--   ALTER TABLE bookings DROP COLUMN IF EXISTS binding_token;
--   ALTER TABLE bookings DROP COLUMN IF EXISTS binding_expires_at;
--   DROP INDEX IF EXISTS bookings_binding_token_uq;
--   DROP INDEX IF EXISTS bookings_handle_lookup_idx;

ALTER TABLE bookings
  ADD COLUMN binding_token      TEXT        NULL,
  ADD COLUMN binding_expires_at TIMESTAMPTZ NULL;

-- A pending link-booking has no slot yet — the customer picks it on EA's
-- web UI. start_at only becomes known when the EA webhook confirms. Drop
-- the NOT NULL so a pending row can exist without a slot, but require a
-- slot once the booking is confirmed/rescheduled (the reminder scheduler
-- and handle-lookup index only scan confirmed rows, so a NULL start_at on
-- a pending row never reaches them).
ALTER TABLE bookings ALTER COLUMN start_at DROP NOT NULL;
ALTER TABLE bookings ADD CONSTRAINT bookings_terminal_needs_start
  CHECK (status NOT IN ('confirmed', 'rescheduled') OR start_at IS NOT NULL);

-- O(1) webhook resolution + duplicate-binding rejection. Partial so the
-- many rows with a NULL token (confirmed / legacy direct-API bookings)
-- don't collide.
CREATE UNIQUE INDEX bookings_binding_token_uq
  ON bookings (binding_token)
  WHERE binding_token IS NOT NULL;

-- Cancel / reschedule lookup: a customer's confirmed upcoming bookings,
-- keyed on the verified handle. start_at trailing so the same index
-- serves "ORDER BY start_at" for the soonest-first listing.
CREATE INDEX bookings_handle_lookup_idx
  ON bookings (tenant_id, channel, customer_handle, start_at)
  WHERE status = 'confirmed';

INSERT INTO schema_migrations (filename) VALUES ('081_bookings_link_binding.sql')
ON CONFLICT (filename) DO NOTHING;
