-- Migration 120 — broadcast_recipients + booking_reminders missing indexes.
-- Ref: audit findings 07-db-migrations.md #5, #6 (2026-07-02 audit).
--
-- broadcast_recipients.audience_member_id is a FK (043_broadcast_recipients.sql
-- line ~62) with no supporting index; lookups/joins by audience member (e.g.
-- "which broadcasts did this contact receive") force a sequential scan.
--
-- booking_reminders.tenant_id (076_booking_reminders.sql) is filtered by the
-- table's RLS policy (077_phase13_grants.sql) on every tenant-scoped query but
-- has no dedicated index; mirrors the existing bookings_tenant_idx pattern
-- from 075_bookings.sql.
--
-- Both are plain (non-unique, non-partial) B-tree indexes — safe to add
-- without CONCURRENTLY at current table sizes; NOTE: apply.py wraps this
-- entire file in one transaction, so CONCURRENTLY is not an option under the
-- current tooling (see plan's Risks section) — if either table grows large
-- enough that a blocking index build becomes a real prod concern, this must
-- be re-cut as a manually-applied CONCURRENTLY statement outside apply.py.
--
-- Rollback (local dev only):
--   DROP INDEX IF EXISTS broadcast_recipients_audience_member_idx;
--   DROP INDEX IF EXISTS booking_reminders_tenant_idx;

CREATE INDEX broadcast_recipients_audience_member_idx
  ON broadcast_recipients (audience_member_id);

CREATE INDEX booking_reminders_tenant_idx
  ON booking_reminders (tenant_id);

INSERT INTO schema_migrations (filename) VALUES ('120_broadcast_and_reminder_indexes.sql')
ON CONFLICT (filename) DO NOTHING;
