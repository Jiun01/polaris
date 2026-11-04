-- Migration 005b — platform_health KMS sentinel (1-row table, service-pool only).
-- Phase 1 / PR-3. Ref: docs/Polaris V2/04b_PHASE_1_DEEP.md §3.4 + 04a_PHASE_0_DEEP.md §9 Q21.
-- Anchors: deep-dive §9 gotcha #4 (KMS regen is catastrophic post-PR-3).
--
-- Schema deviation from deep-dive: deep-dive listed `kms_sentinel_aad BYTEA NOT NULL`
-- so the AAD travels with the ciphertext. We drop the column — the platform
-- AAD is a code constant (`PLATFORM_SENTINEL_AAD` in services/secrets.py),
-- so storing it per-row would just persist a hardcoded value with no
-- operational signal. If the AAD scheme ever rotates we ship a new sentinel
-- ciphertext under the new AAD via the seed CLI; old sentinels are simply
-- overwritten.
--
-- The id CHECK + PRIMARY KEY (id = 1) enforces single-row table — UPSERTs
-- always target id=1, INSERTs of other ids fail loudly. Idempotent.
--
-- No RLS: this table is platform-scoped and only accessed via service_pool
-- (BYPASSRLS). Grants in 011_secrets_grants.sql restrict it to service_pool
-- exclusively (no GRANTs to polaris_anon).
--
-- Rollback (local only):
--   DROP TABLE IF EXISTS platform_health;

CREATE TABLE platform_health (
  id                            INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  kms_sentinel_ciphertext       BYTEA NOT NULL,
  kms_sentinel_plaintext_hash   BYTEA NOT NULL,
  last_verified_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (filename) VALUES ('005b_platform_health.sql')
ON CONFLICT (filename) DO NOTHING;
