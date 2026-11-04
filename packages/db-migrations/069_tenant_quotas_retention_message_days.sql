-- Migration 069 — Add retention_message_days column to tenant_quotas.
--
-- tenant_settings.py _GET_SQL queries COALESCE(tq.retention_message_days, 90)
-- but the column was never added to the migration. Without it, any GET or
-- PATCH to /api/tenant/settings crashes with an asyncpg UndefinedColumnError.
--
-- Default: 90 days (matches the COALESCE fallback already in the query).
--
-- Callers: apps/api/polaris_api/routers/tenant_settings.py _GET_SQL
-- Rollback (local only):
--   ALTER TABLE tenant_quotas DROP COLUMN IF EXISTS retention_message_days;

ALTER TABLE tenant_quotas
  ADD COLUMN IF NOT EXISTS retention_message_days INT NOT NULL DEFAULT 90;
