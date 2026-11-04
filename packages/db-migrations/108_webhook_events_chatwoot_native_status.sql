-- Migration 108 — extend webhook_events.status CHECK with the Chatwoot-native
-- consumer statuses: 'human_handling' + 'no_recipient' + 'chatwoot_deferred'.
--
-- Stage 4 adds 'chatwoot_deferred': when tenants.settings.use_chatwoot_inbound
-- is on, meta_router defers the message to Chatwoot's agent-bot path instead of
-- dispatching. The forensic row makes that deferral visible to ops (and surfaces
-- any message still hitting meta_router for an already-migrated tenant).
--
-- Chatwoot-native rehaul (Stage 1). The agent-bot fires message_created on
-- EVERY inbound regardless of who owns the conversation, so the Polaris
-- consumer gates bot processing on conversation.status == 'pending':
--   * a human owns the conversation (open/resolved/snoozed) → bot stays
--     silent and records a forensic 'human_handling' row;
--   * the reply recipient (conversation.contact_inbox.source_id) can't be
--     resolved → records 'no_recipient' and skips.
-- Both are new statuses, so the CHECK from migrations 015 + 017 must widen.
--
-- Append-only policy (repo policy): drop + re-add the CHECK with the new values,
-- atomic inside apply.py's per-migration transaction (a mid-flight error
-- leaves the original CHECK in place).
--
-- Rollback (local only):
--   ALTER TABLE webhook_events
--     DROP CONSTRAINT webhook_events_status_check,
--     ADD CONSTRAINT webhook_events_status_check CHECK (status IN (
--       'received','processed','tenant_not_found','suspended_quota',
--       'suspended_payment','suspended_deleted','replay_ignored',
--       'demo_routed','hmac_fail','non_message_event'
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
    'non_message_event',
    'human_handling',
    'no_recipient',
    'chatwoot_deferred'
  ));

INSERT INTO schema_migrations (filename) VALUES ('108_webhook_events_chatwoot_native_status.sql')
ON CONFLICT (filename) DO NOTHING;
