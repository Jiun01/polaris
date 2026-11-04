-- Migration 100 — DELETE grants for polaris_service on hard-delete tables.
--
-- The admin hard-delete endpoint (DELETE /api/admin/tenants/{id}/purge) must
-- physically remove rows from tables whose FK to tenants(id) has no CASCADE.
-- Those tables were created without DELETE grants for polaris_service (some
-- intentionally append-only, others simply never needed DELETE at the time).
--
-- Tables covered:
--   audit_logs          — migration 012 granted only INSERT + SELECT (append-only
--                         policy); hard-delete is the one sanctioned exception.
--   audience_members    — migration 009 had no GRANT block; FK has no CASCADE.
--   broadcast_recipients — migration 043; FK to audience_members has no CASCADE,
--                          so must be deleted before audience_members.
--   demo_sessions       — migration 008b; FK has no CASCADE.
--   tenant_invoices     — migration 008; FK has no CASCADE.
--
-- polaris_anon is deliberately excluded — tenant-scope code must never be
-- able to hard-delete rows; that right belongs to the admin service pool only.

GRANT DELETE ON audit_logs            TO polaris_service;
GRANT DELETE ON audience_members      TO polaris_service;
GRANT DELETE ON broadcast_recipients  TO polaris_service;
GRANT DELETE ON demo_sessions         TO polaris_service;
GRANT DELETE ON tenant_invoices       TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('100_hard_delete_grants.sql')
ON CONFLICT (filename) DO NOTHING;
