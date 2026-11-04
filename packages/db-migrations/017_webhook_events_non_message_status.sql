-- Migration 017 — extend webhook_events.status CHECK with 'non_message_event'.
-- Phase 2 / PR-7 (gap fixes). Ref: docs/Polaris V2/04c_PHASE_2_DEEP.md §10
-- + Phase 2 gap audit B1.
--
-- Why: Meta POSTs four classes of event to the same /webhooks/meta URL:
--   1. `messages`              — inbound user message (Phase 2 PROCESSES)
--   2. `statuses`              — outbound delivery state (Phase 6)
--   3. `message_template_status_update` — template approval lifecycle (Phase 6)
--   4. `account_review_update` / `account_alerts` — WABA health (Phase 10)
--
-- PR-2/3/4 only extracted class 1; classes 2-4 authenticated → resolved
-- tenant → _extract_messages returned [] → loop exited → 200 response
-- with ZERO webhook_events rows written. Ops had no signal that Meta
-- was even delivering them. This migration adds a new status enum so
-- the dispatcher can write a per-entry forensic row for non-message
-- events, separately countable from genuine 'received' messages.
--
-- Append-only migration policy (repo policy): rather than a new table or
-- a destructive ALTER, this drops + re-adds the existing CHECK
-- constraint with the new value included. The drop+add is wrapped in a
-- single ALTER TABLE so it's atomic + applied via the apply.py
-- per-migration transaction (so a mid-flight error leaves the original
-- CHECK in place).
--
-- Rollback (local only):
--   ALTER TABLE webhook_events
--     DROP CONSTRAINT webhook_events_status_check,
--     ADD CONSTRAINT webhook_events_status_check CHECK (status IN (
--       'received', 'processed', 'tenant_not_found',
--       'suspended_quota', 'suspended_payment', 'suspended_deleted',
--       'replay_ignored', 'demo_routed', 'hmac_fail'
--     ));

ALTER TABLE webhook_events
  DROP CONSTRAINT webhook_events_status_check,
  ADD CONSTRAINT webhook_events_status_check CHECK (status IN (
    'received',
    'processed',
    'tenant_not_found',
    'suspended_quota',
    'suspended_payment',
    'suspended_deleted',
    'replay_ignored',
    'demo_routed',
    'hmac_fail',
    'non_message_event'
  ));

INSERT INTO schema_migrations (filename) VALUES ('017_webhook_events_non_message_status.sql')
ON CONFLICT (filename) DO NOTHING;
