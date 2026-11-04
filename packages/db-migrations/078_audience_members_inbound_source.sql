-- Migration 078 — allow opt_in_source='inbound' on audience_members.
-- Ref: services/audience/members.py::upsert_audience_member (Phase 14b gap).
--
-- Migration 009 created audience_members with a CHECK restricting
-- opt_in_source to ('demo_session','csv_upload','api_sync') — the three
-- sources that existed when audience rows were only created by the
-- portal CSV importer, the demo system, or external API sync.
--
-- The pipeline now upserts an audience_members row on a customer's
-- FIRST inbound message (so organically-acquired contacts appear in the
-- audience screen without a CSV import). That origin is none of the
-- three existing sources, so we add 'inbound' as a fourth valid value.
-- 'inbound' is distinguishable in analytics (organic acquisition vs
-- imported/synced lists) and keeps the opt-in provenance honest.
--
-- The CHECK from 009 is an inline column constraint, auto-named
-- audience_members_opt_in_source_check. We drop + re-add it with the
-- expanded value set. Idempotent: DROP ... IF EXISTS + the new name is
-- stable across re-applies.
--
-- Rollback (local only):
--   ALTER TABLE audience_members
--     DROP CONSTRAINT audience_members_opt_in_source_check,
--     ADD  CONSTRAINT audience_members_opt_in_source_check
--       CHECK (opt_in_source IN ('demo_session','csv_upload','api_sync'));

ALTER TABLE audience_members
  DROP CONSTRAINT IF EXISTS audience_members_opt_in_source_check;

ALTER TABLE audience_members
  ADD CONSTRAINT audience_members_opt_in_source_check
    CHECK (opt_in_source IN ('demo_session','csv_upload','api_sync','inbound'));

INSERT INTO schema_migrations (filename) VALUES ('078_audience_members_inbound_source.sql')
ON CONFLICT (filename) DO NOTHING;
