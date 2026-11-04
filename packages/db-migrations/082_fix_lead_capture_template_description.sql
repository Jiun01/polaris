-- Migration 082 — fix whitelabel leak in lead-capture-handoff template description.
--
-- The 'lead-capture-handoff' flow template description contained the internal
-- tool name "Chatwoot" (a customer-facing whitelabel violation found by E2E
-- assertNoBrandLeak on /flows). Replace with the whitelabelled term
-- "messaging inbox" to match the rest of the portal copy.
--
-- Migration 038 uses ON CONFLICT DO UPDATE, so we cannot simply re-apply it;
-- this targeted UPDATE is the correct append-only fix.

UPDATE flow_templates
SET
    description = 'Tags high-intent inbounds as leads and hands off to the sales team in the messaging inbox.',
    last_updated_at = now()
WHERE slug = 'lead-capture-handoff'
  AND description LIKE '%Chatwoot%';

INSERT INTO schema_migrations (filename) VALUES ('082_fix_lead_capture_template_description.sql')
ON CONFLICT (filename) DO NOTHING;
