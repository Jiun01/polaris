-- Migration 046 — expand webhook_events.source CHECK for Phase 6.
-- Phase 6 / PR-3. Ref: docs/Polaris V2/04g_PHASE_6_DEEP.md §6.2.
--
-- Phase 1 / migration 015 created webhook_events with CHECK source IN
-- ('meta','chatwoot'). Phase 6 adds three new inbound webhook routes
-- per the Meta Graph API surfaces we now consume:
--
--   meta-template-status — template lifecycle (APPROVED/REJECTED/...)
--   wa-status            — per-message delivery state (sent/delivered/
--                          read/failed) for outbound broadcast recipients
--   wa-account           — MM Lite onboarding events (AD_ACCOUNT_LINKED,
--                          ONBOARDING_COMPLETED, AD_ACCOUNT_UNLINKED)
--
-- Each gets its own source value rather than nesting under 'meta'. Two
-- reasons:
--   (1) Idempotency key shapes are radically different — message-level
--       UNIQUE on wamid for wa-status, template+timestamp tuple for
--       template-status, phone+event+timestamp for wa-account. Keeping
--       sources separate keeps the (source, external_id) UNIQUE
--       meaningful.
--   (2) Operational observability — admins reading webhook_events to
--       triage "why didn't this template approve?" want to filter on
--       source='meta-template-status' not source='meta' with a regex
--       on external_id.
--
-- Idempotent: drops the existing constraint by name, re-adds with the
-- expanded set. Apply order via schema_migrations.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   ALTER TABLE webhook_events DROP CONSTRAINT webhook_events_source_check;
--   ALTER TABLE webhook_events ADD CONSTRAINT webhook_events_source_check
--     CHECK (source IN ('meta','chatwoot'));

ALTER TABLE webhook_events
  DROP CONSTRAINT IF EXISTS webhook_events_source_check;

ALTER TABLE webhook_events
  ADD CONSTRAINT webhook_events_source_check
  CHECK (source IN (
    'meta',
    'chatwoot',
    'meta-template-status',
    'wa-status',
    'wa-account'
  ));

INSERT INTO schema_migrations (filename) VALUES ('046_webhook_events_source_expansion.sql')
ON CONFLICT (filename) DO NOTHING;
