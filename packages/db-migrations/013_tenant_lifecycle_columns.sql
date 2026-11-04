-- Migration 013 — tenant lifecycle columns (ALTER tenants).
-- Phase 1 / PR-5. Ref: docs/Polaris V2/04b_PHASE_1_DEEP.md §3.4 + ADR-029.
-- Anchors: ADR-029 (tenant lifecycle: sales-led provisioning, suspension
--          per ADR-018/049, soft-delete at end of paid period, 30-day
--          recovery window, hard purge after).
--
-- Existing lifecycle facts already on `tenants` (from migration 002):
--   - status enum: active|suspended|deleted
--   - suspended_reason: quota_exhausted|payment_overdue|NULL
--   - subscription_start, subscription_end
--   - billing_status
--
-- This migration adds the remaining two timestamps:
--   - deleted_at   — when the tenant was soft-deleted (status flipped to
--                    'deleted'). Drives the 30-day recovery countdown.
--   - purge_at     — when the hard-purge sweeper is scheduled to run
--                    (typically deleted_at + 30 days). Recovery is allowed
--                    while now() < purge_at. After hard purge, the tenant
--                    row + all FK-cascaded data are gone.
--
-- Both columns are nullable (active/suspended tenants have neither set).
-- Phase 10's lifecycle sweeper:
--   1. Reads tenants WHERE status='active' AND subscription_end < now() AND
--      billing_status != 'paid' — these go to status='suspended',
--      suspended_reason='payment_overdue' per ADR-049 ramp.
--   2. After D+31 lockdown (per ADR-049), tenant moves to status='deleted',
--      deleted_at = now(), purge_at = now() + 30 days.
--   3. Each night, tenants WHERE status='deleted' AND purge_at < now() are
--      hard-purged (DELETE; FK CASCADE cleans up all per-tenant data).
--
-- Rollback (local only):
--   ALTER TABLE tenants DROP COLUMN IF EXISTS deleted_at;
--   ALTER TABLE tenants DROP COLUMN IF EXISTS purge_at;

ALTER TABLE tenants
  ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS purge_at   TIMESTAMPTZ NULL;

-- Sanity CHECK: a row with status='deleted' should have deleted_at + purge_at
-- set; otherwise the sweeper can't find it. Enforced as an invariant to catch
-- application bugs that flip status without populating the timestamps.
ALTER TABLE tenants
  ADD CONSTRAINT tenants_deleted_state_complete
  CHECK (
    (status = 'deleted' AND deleted_at IS NOT NULL AND purge_at IS NOT NULL)
    OR status <> 'deleted'
  );

-- Index for the nightly purge sweeper.
CREATE INDEX IF NOT EXISTS tenants_purge_at_idx
  ON tenants (purge_at)
  WHERE status = 'deleted';

INSERT INTO schema_migrations (filename) VALUES ('013_tenant_lifecycle_columns.sql')
ON CONFLICT (filename) DO NOTHING;
