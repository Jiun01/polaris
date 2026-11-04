-- Migration 047 — webhook_events_outbound (deferred-dispatch event sink).
-- Phase 6 / PR-8. Ref: docs/Polaris V2/04g_PHASE_6_DEEP.md §6.3.
-- Anchors: ADR-035 Amendment evening (outbound webhook DISPATCH deferred to
--          v3.0; rows are still persisted at GA so admin tooling can inspect
--          what would fan out + the v3.0 dispatcher can backfill from a
--          cutoff date per Q-PHASE6-02).
--
-- Four event types currently emitted at GA:
--   * template.<status>         — services/templates/lifecycle.py
--                                 (template.approved | rejected | paused
--                                  | disabled | pending)
--   * audience.opted_out        — services/audience/opt_out.mark_opted_out
--   * broadcast.completed       — services/broadcast/worker._mark_completed
--   * broadcast.failed          — services/broadcast/worker._mark_errored
--   * channel.mm_lite.<state>   — webhooks/wa_account_router._apply_mm_lite_update
--                                 (in_progress | onboarded | unlinked)
--
-- Each emission site currently wraps the INSERT in a try/UndefinedTableError
-- guard so fixtures that don't run migrations still work. After this
-- migration applies the guard becomes a no-op — kept as defence-in-depth.
--
-- At GA there is NO dispatcher worker. Rows pile up; admin tooling can
-- read them; the v3.0 outbound-dispatch worker reads forward from a
-- launch_date cutoff (Q-PHASE6-02 — don't backfill historical events).
--
-- Schema choices:
--   * event_type TEXT — no CHECK constraint because the set evolves
--     (template.* names, channel.mm_lite.* names, future broadcast.*
--     sub-events). Drift surfaces via test_outbound_event_types.py
--     pinning the emission strings.
--   * payload JSONB — opaque to the dispatcher; carrier-specific shape
--     per event type.
--   * No dispatched_at column at GA — the v3.0 dispatcher will add it
--     in its own migration along with retries / dead-letter columns.
--   * (tenant_id, created_at) index for admin tooling "what's queued
--     for this tenant".
--
-- RLS: tenant-scoped read; INSERT happens from the service pool
-- (broadcast worker, webhook handlers, audience opt_out path). Tenant
-- can SELECT their own events for audit; UPDATE / DELETE forbidden
-- (the dispatcher is platform-owned).
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   DROP TABLE IF EXISTS webhook_events_outbound CASCADE;

CREATE TABLE webhook_events_outbound (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  event_type   TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 200),
  payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX webhook_events_outbound_tenant_created_idx
  ON webhook_events_outbound (tenant_id, created_at DESC);

CREATE INDEX webhook_events_outbound_event_type_idx
  ON webhook_events_outbound (event_type, created_at DESC);

ALTER TABLE webhook_events_outbound ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_events_outbound FORCE  ROW LEVEL SECURITY;

CREATE POLICY webhook_events_outbound_self_select ON webhook_events_outbound
  FOR SELECT
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- polaris_anon: tenant SELECT only (audit / portal display).
GRANT SELECT ON webhook_events_outbound TO polaris_anon;
-- polaris_service: full access (the writers go through service pool,
-- and the v3.0 dispatcher will too).
GRANT ALL ON webhook_events_outbound TO polaris_service;

INSERT INTO schema_migrations (filename) VALUES ('047_webhook_events_outbound.sql')
ON CONFLICT (filename) DO NOTHING;
