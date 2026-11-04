-- Migration 056 -- Phase 9 PR-5: impersonation columns on audit_logs.
-- Adds impersonating_kind + impersonating_id to audit_logs so impersonated
-- writes carry both actor_id (platform_admin) and impersonating_id (tenant user).
-- services/audit.py::record() accepts these as optional kwargs.
-- Both columns NULL for non-impersonated writes.
--
-- Rollback (local dev only):
--   ALTER TABLE audit_logs
--     DROP COLUMN IF EXISTS impersonating_kind,
--     DROP COLUMN IF EXISTS impersonating_id;

ALTER TABLE audit_logs
  ADD COLUMN IF NOT EXISTS impersonating_kind TEXT NULL,
  ADD COLUMN IF NOT EXISTS impersonating_id   UUID NULL;

COMMENT ON COLUMN audit_logs.impersonating_kind IS
  'Phase 9 PR-5 -- e.g. \"platform_admin\". NULL for non-impersonated writes.';
COMMENT ON COLUMN audit_logs.impersonating_id IS
  'Phase 9 PR-5 -- UUID of the impersonating entity. NULL for normal writes.';

INSERT INTO schema_migrations (filename) VALUES ('056_phase9_audit_impersonation_columns.sql')
ON CONFLICT (filename) DO NOTHING;
