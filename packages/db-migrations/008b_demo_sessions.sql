-- Migration 008b — demo_sessions + polaris-internal tenant seed.
-- Phase 1 / PR-5. Ref: docs/Polaris V2/05_DATA_MODEL_AND_API.md §4.5 + 04b §3.4.
-- Anchors: ADR-027 (demo mode restored — 6h / 50-msg sessions via Polaris's
--          shared demo WABA +60 11-5458 1688), ADR-038 (90-day auto-purge
--          after expiry; DSAR export/delete includes demo data within window),
--          ADR-040 (polaris-internal tenant — all demo rows scoped here),
--          ADR-046 (time + message warning columns).
--
-- Schema notes:
--   - auto_purge_at is computed by a BEFORE INSERT/UPDATE trigger: always
--     `expires_at + 90 days`. The deep-dive's GENERATED STORED approach
--     fails because Postgres requires generation expressions to be
--     IMMUTABLE — interval arithmetic on TIMESTAMPTZ is only STABLE
--     (`generation expression is not immutable` error). A DEFAULT
--     expression can't reference another column. A trigger is the
--     idiomatic way to derive one timestamp column from another while
--     keeping the column queryable + indexable.
--   - Partial UNIQUE on `(channel_user_id, demo_code) WHERE is_expired = false`
--     enforces "one active demo per (handle, demo_code)" without blocking
--     expired re-entries — sales agents demo to the same prospect multiple
--     times under the same demo_code.
--   - chatwoot_* columns are nullable: the row is created at activation,
--     Chatwoot conversation/contact land later via the demo router.
--
-- polaris-internal tenant: ADR-040 says demo_sessions all live under a single
-- platform tenant. We seed it here with a fixed UUID for cross-environment
-- referenceability. Slug is "polaris-internal" — sufficiently distinctive
-- to never collide with a real customer slug.
--
-- RLS posture: tenant_id-scoped. Demo handlers set
-- app.current_tenant_id = '<polaris-internal UUID>' before reading/writing.
-- The polaris-internal UUID is referenced from app code via a config constant.
--
-- Rollback (local only):
--   DROP TABLE IF EXISTS demo_sessions;
--   DELETE FROM tenants WHERE id = '00000000-0000-0000-0000-000000000001';

-- Seed the platform tenant FIRST so the FK on demo_sessions resolves.
INSERT INTO tenants (id, slug, status, plan_tier, timezone)
VALUES (
  '00000000-0000-0000-0000-000000000001',
  'polaris-internal',
  'active',
  'enterprise',
  'Asia/Kuala_Lumpur'
)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE demo_sessions (
  id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                 UUID NOT NULL REFERENCES tenants(id),
  channel                   TEXT NOT NULL,
  channel_user_id           TEXT NOT NULL,
  channel_user_name         TEXT NULL,
  demo_code                 TEXT NOT NULL,
  message_count             INT  NOT NULL DEFAULT 0,
  max_messages              INT  NOT NULL DEFAULT 50,
  activated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at                TIMESTAMPTZ NOT NULL,
  auto_purge_at             TIMESTAMPTZ NOT NULL,            -- set by trigger; see _set_demo_auto_purge_at
  is_expired                BOOLEAN NOT NULL DEFAULT false,
  time_warning_sent_at      TIMESTAMPTZ NULL,
  message_warning_sent_at   TIMESTAMPTZ NULL,
  chatwoot_conversation_id  INT NULL,
  chatwoot_contact_id       INT NULL,
  chatwoot_inbox_id         INT NULL
);

-- Derive auto_purge_at from expires_at on every INSERT/UPDATE. Function is
-- VOLATILE (default) because it writes to NEW; SECURITY INVOKER (default)
-- so it runs with the caller's privileges — no cross-tenant escalation.
CREATE OR REPLACE FUNCTION _set_demo_auto_purge_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.auto_purge_at := NEW.expires_at + INTERVAL '90 days';
  RETURN NEW;
END;
$$;

CREATE TRIGGER demo_sessions_set_auto_purge_at
  BEFORE INSERT OR UPDATE OF expires_at ON demo_sessions
  FOR EACH ROW
  EXECUTE FUNCTION _set_demo_auto_purge_at();

CREATE UNIQUE INDEX uniq_demo_session_active
  ON demo_sessions (channel_user_id, demo_code)
  WHERE is_expired = false;

CREATE INDEX demo_sessions_tenant_activated_idx
  ON demo_sessions (tenant_id, activated_at);

-- Index for the nightly purge sweeper (Phase 4/10 cron).
CREATE INDEX demo_sessions_auto_purge_idx
  ON demo_sessions (auto_purge_at)
  WHERE is_expired = true;

ALTER TABLE demo_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_sessions FORCE  ROW LEVEL SECURITY;

CREATE POLICY demo_sessions_self_all ON demo_sessions
  FOR ALL
  USING       (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK  (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

INSERT INTO schema_migrations (filename) VALUES ('008b_demo_sessions.sql')
ON CONFLICT (filename) DO NOTHING;
