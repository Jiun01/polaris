-- Migration 080 — branch-centric EA booking routing (v2.1 / PR-1).
-- Ref: docs/Polaris V2/09_DECISION_LOG.md (branch-centric EA routing ADR).
--
-- Wires the missing "inbox -> branch -> EA calendar" chain:
--
--   * tenant_channels.tenant_branch_id — the inbox (one tenant_channels
--     row per WhatsApp/IG/Messenger surface) now declares which branch
--     it belongs to. The inbound pipeline copies this onto the
--     conversation at creation so downstream flow nodes know the branch
--     without re-resolving the channel.
--
--   * tenant_branches.ea_provider_id / ea_default_service_id — promote
--     EA routing out of the loose metadata JSONB (where the E2E seed
--     wrote ea_provider_id but nothing READ it) into first-class,
--     typed columns. The booking node reads these to target the right
--     EA calendar; when NULL it falls back to the flow-hardcoded
--     service_id/provider_id (GA behaviour preserved).
--
-- Resolution order in the booking node (see services/flow_runtime/
-- nodes/booking.py): branch EA config -> flow-node hardcoded ids.
-- The service_id stays "what you're booking" (flow-level); the
-- provider_id / default service come from the branch "where".
--
-- Grants: tenant_channels + tenant_branches already carry table-level
-- grants (migrations 004 / 017b); new columns inherit them, so no
-- GRANT changes are needed here.
--
-- Rollback (local dev only):
--   ALTER TABLE tenant_channels DROP COLUMN IF EXISTS tenant_branch_id;
--   ALTER TABLE tenant_branches DROP COLUMN IF EXISTS ea_provider_id;
--   ALTER TABLE tenant_branches DROP COLUMN IF EXISTS ea_default_service_id;
--   DROP INDEX IF EXISTS tenant_channels_branch_idx;

-- -------------------------------------------------------------------------
-- tenant_channels: link each inbox to a branch.
-- ON DELETE SET NULL — deleting a branch must not cascade-delete the
-- inbox row (the channel keeps working; it just loses branch routing
-- and falls back to flow-hardcoded EA ids).
-- -------------------------------------------------------------------------
ALTER TABLE tenant_channels
  ADD COLUMN tenant_branch_id UUID NULL
    REFERENCES tenant_branches(id) ON DELETE SET NULL;

CREATE INDEX tenant_channels_branch_idx
  ON tenant_channels (tenant_branch_id)
  WHERE tenant_branch_id IS NOT NULL;

-- -------------------------------------------------------------------------
-- tenant_branches: first-class EA routing columns.
-- ea_provider_id        — the EA provider/calendar bookings on this
--                         branch's inbox(es) land in.
-- ea_default_service_id — optional branch-default service; the flow
--                         node's service_id still wins when set, this
--                         is the fallback when a flow doesn't pin one.
-- Both NULL -> booking node uses the flow-hardcoded ids (GA behaviour).
-- EA enforces tenant-scoping server-side via the JWT tenant_id claim;
-- these ids are validated there, not by a DB FK (EA is a separate DB).
-- -------------------------------------------------------------------------
ALTER TABLE tenant_branches
  ADD COLUMN ea_provider_id        INT NULL CHECK (ea_provider_id IS NULL OR ea_provider_id >= 1),
  ADD COLUMN ea_default_service_id INT NULL CHECK (ea_default_service_id IS NULL OR ea_default_service_id >= 1);

INSERT INTO schema_migrations (filename) VALUES ('080_branch_ea_routing.sql')
ON CONFLICT (filename) DO NOTHING;
