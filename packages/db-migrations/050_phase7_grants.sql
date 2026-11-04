-- Migration 050 — Phase 7 grants.
-- Phase 7 / PR-1. Ref: docs/Polaris V2/04h_PHASE_7_DEEP.md §3.2.
-- Pairs with 049.
--
-- No new tables in Phase 7 — only columns added to tenant_channels +
-- conversations (migration 049). The existing tenant-isolation policies
-- on both tables cover the new columns automatically (PostgreSQL applies
-- row-level policies, not column-level).
--
-- One column-level grant is needed: polaris_service must UPDATE the
-- post-push write-back columns on tenant_channels, because the push
-- happens from services/messenger_profile.py through the service pool
-- (no-RLS, GUC-honour invariant) — not through the portal-JWT path.
--
-- Q-PHASE7-01 dropped the originally-planned tenant_secrets grant for
-- chatwoot_bot_access_token because the bot token is now a platform-wide
-- env var (CHATWOOT_BOT_ACCESS_TOKEN) rather than per-tenant in
-- tenant_secrets. Chatwoot agent_bots issue global tokens that attach
-- across all chatwoot_account_id values via the URL composition.
--
-- Rollback (local dev only):
--   REVOKE UPDATE (messenger_profile_last_pushed_at, messenger_profile_last_push_error)
--     ON tenant_channels FROM polaris_service;

-- ---------------------------------------------------------------------
-- tenant_channels — polaris_service column-level UPDATE on push-state
-- columns. polaris_service already has ALL on tenant_channels from
-- migration 004; this is defence-in-depth: if a future migration
-- tightens polaris_service to column-level grants, the push paths
-- explicitly survive.
-- ---------------------------------------------------------------------
GRANT UPDATE (messenger_profile_last_pushed_at, messenger_profile_last_push_error)
  ON tenant_channels TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('050_phase7_grants.sql')
ON CONFLICT (filename) DO NOTHING;
