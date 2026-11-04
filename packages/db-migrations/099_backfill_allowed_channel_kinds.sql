-- Migration 099 — backfill allowed_channel_kinds for pro/ultra/enterprise tenants.
--
-- All tiers were provisioned with ["whatsapp"] only. Channels added later
-- (instagram, messenger — Phase 10; telegram — off-meta M2) were never backfilled
-- into existing tenant_quotas rows. This migration brings existing tenants in line
-- with the correct per-tier defaults.
--
-- Starter stays ["whatsapp"] (1-inbox cap enforced separately in provision.py).
-- Pro/Ultra/Enterprise get all GA provisionable kinds.
--
-- Rollback (local only):
--   UPDATE tenant_quotas
--   SET allowed_channel_kinds = ARRAY['whatsapp']
--   WHERE plan_tier IN ('pro', 'ultra', 'enterprise');

UPDATE tenant_quotas
SET    allowed_channel_kinds = ARRAY['whatsapp', 'instagram', 'messenger', 'telegram', 'email']
WHERE  plan_tier IN ('pro', 'ultra', 'enterprise')
  AND  allowed_channel_kinds = ARRAY['whatsapp'];

INSERT INTO schema_migrations (filename) VALUES ('099_backfill_allowed_channel_kinds.sql')
ON CONFLICT (filename) DO NOTHING;
