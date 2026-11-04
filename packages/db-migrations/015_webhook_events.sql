-- Migration 015 — webhook_events (Phase 2 / PR-2).
-- Ref: docs/Polaris V2/04c_PHASE_2_DEEP.md §3.1 + 05_DATA_MODEL_AND_API.md §2 row 007.
--
-- Inbound webhook idempotency + audit-grade payload-hash table.
--
--   (source, external_id) UNIQUE  is the durable de-dup floor. Redis SETNX
--   in services/idempotency.py (PR-4) is the fast-path that catches
--   replays before we touch Postgres at all.
--
--   tenant_id is NULLABLE because rows from HMAC-fail and unknown-tenant
--   payloads land here too (the row exists so we can investigate
--   suspected spoofing later). The row gets a tenant once resolution
--   succeeds; if it never does, tenant_id stays NULL.
--
--   payload_hash = sha256(raw_request_body). Distinct from the HMAC: the
--   HMAC was the auth gate; payload_hash is the forensic fingerprint
--   attached to the row so a later investigation can prove "this is the
--   same byte sequence we received."
--
--   external_id semantics (PR-2): sha256(raw_body) hex — naturally
--   dedupes byte-identical re-deliveries. PR-4 narrows to per-message
--   wamid keys for true per-message replay defence. The text column lets
--   us swap granularity without a migration.
--
-- RLS posture:
--   - ENABLE + FORCE.
--   - SELECT: tenant-scoped via current_setting; unresolved rows
--     (tenant_id IS NULL) are platform-only — service_pool sees them for
--     ops + spoof investigations, polaris_anon sees none.
--   - INSERT via polaris_anon requires tenant_id match (defence-in-
--     depth on the per-tenant write path). Unresolved-row inserts go via
--     service_pool (BYPASSRLS) — which is exactly what
--     services/webhook_resolver.py does (the only Phase 2 surface on
--     the lint allowlist).
--   - UPDATE/DELETE: none. Append-only. Cleanup is via the Phase 10
--     retention sweeper (not part of GA).
--
-- Grants live in 016_webhook_events_grants.sql (mirrors the Phase 1
-- pattern of 011_secrets_grants.sql / 012_audit_grants.sql).
--
-- Rollback (local only):
--   DROP TABLE IF EXISTS webhook_events CASCADE;

CREATE TABLE webhook_events (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  source        TEXT         NOT NULL CHECK (source IN ('meta', 'chatwoot')),
  external_id   TEXT         NOT NULL,
  tenant_id     UUID         NULL REFERENCES tenants(id) ON DELETE CASCADE,
  channel_id    UUID         NULL REFERENCES tenant_channels(id) ON DELETE SET NULL,
  status        TEXT         NOT NULL CHECK (status IN (
                  'received',
                  'processed',
                  'tenant_not_found',
                  'suspended_quota',
                  'suspended_payment',
                  'suspended_deleted',
                  'replay_ignored',
                  'demo_routed',
                  'hmac_fail'
                )),
  error_class   TEXT         NULL,
  payload_hash  BYTEA        NOT NULL CHECK (length(payload_hash) = 32),
  received_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
  CONSTRAINT webhook_events_source_external_id_unique UNIQUE (source, external_id)
);

-- Tenant-scoped recency lookup ("show me the last N webhook events for
-- my tenant"). Excludes unresolved rows since tenants can't see those.
CREATE INDEX webhook_events_tenant_received_idx
  ON webhook_events (tenant_id, received_at DESC)
  WHERE tenant_id IS NOT NULL;

-- Platform ops slice: "show me all hmac_fails / tenant_not_founds in
-- the last hour." Used by Phase 9 admin + alerting.
CREATE INDEX webhook_events_status_received_idx
  ON webhook_events (status, received_at DESC);

ALTER TABLE webhook_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_events FORCE  ROW LEVEL SECURITY;

-- Resolved rows are visible to that tenant. Unresolved (tenant_id NULL)
-- rows are platform-only — service_pool sees them via BYPASSRLS.
CREATE POLICY webhook_events_self_select ON webhook_events
  FOR SELECT
  USING (
    tenant_id IS NOT NULL
    AND tenant_id = current_setting('app.current_tenant_id', true)::uuid
  );

-- Per-tenant write path (PR-4 will use this for `processed` and
-- `replay_ignored` rows from within a TenantContext transaction).
-- PR-2's unresolved-row writes (hmac_fail, tenant_not_found) go via
-- service_pool (BYPASSRLS), bypassing this policy.
CREATE POLICY webhook_events_self_insert ON webhook_events
  FOR INSERT
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

INSERT INTO schema_migrations (filename) VALUES ('015_webhook_events.sql')
ON CONFLICT (filename) DO NOTHING;
