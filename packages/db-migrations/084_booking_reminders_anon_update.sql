-- Migration 084 — let the bot (polaris_anon) void reminders on cancel/reschedule.
--
-- The conversational cancel/reschedule handler (services/booking/conversational.py)
-- runs inside the inbound pipeline under polaris_anon and calls
-- cancel_booking_reminders(), which UPDATEs booking_reminders (status →
-- 'cancelled'). Migration 077 granted polaris_anon SELECT only, so that UPDATE
-- raised "permission denied", which poisoned the pipeline transaction and rolled
-- back the booking-status flip done earlier in the same transaction (the customer
-- saw "cancelled" but the Polaris row stayed 'confirmed'; the EA appointment was
-- already gone — a data-consistency bug).
--
-- The conversational handler now also SAVEPOINT-isolates that cleanup so a failure
-- can't poison the outer transaction; this grant additionally lets the void
-- actually succeed. The existing booking_reminders_tenant_isolation RLS policy
-- (077) is FOR ALL with USING + WITH CHECK on app.current_tenant_id, so writes
-- stay tenant-scoped. Append-only + idempotent (GRANT is idempotent).

GRANT UPDATE ON booking_reminders TO polaris_anon;

INSERT INTO schema_migrations (filename) VALUES ('084_booking_reminders_anon_update.sql')
ON CONFLICT (filename) DO NOTHING;
