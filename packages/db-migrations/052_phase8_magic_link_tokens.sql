-- Migration 052 — Phase 8 PR-2 portal magic-link token storage.
-- Ref: docs/Polaris V2/04i_PHASE_8_DEEP.md §5.1 (NextAuth options) +
--      Q-PHASE8-01 (NextAuth EmailProvider, reuse Chatwoot SMTP).
-- Anchors:
--   - ADR-030 (magic-link only, 15-min expiry, tenant_admin = only role).
--   - ADR-052 (no public signup at GA — signIn callback rejects emails
--     without a corresponding tenant_users row).
--
-- NextAuth v4 EmailProvider stores one-time verification tokens here.
-- We implement a minimal custom NextAuth Adapter (apps/portal/lib/auth/adapter.ts)
-- that uses ONLY this table for token CRUD + JOINs auth.users for the user
-- lookup. We do NOT use NextAuth's full schema (users/accounts/sessions)
-- because:
--
--   - The source of truth for "is this email a valid tenant_admin?" is
--     auth.users JOIN tenant_users (Phase 1 migration 003). Duplicating
--     user state into next_auth.users would create a drift surface.
--
--   - JWT session strategy means NextAuth never touches its `sessions`
--     table — JWT lives entirely in the browser cookie.
--
--   - `accounts` is for OAuth providers; we ship magic-link only at GA.
--
-- So one table is enough.
--
-- Access:
--   - polaris_service (BYPASSRLS) — full CRUD; the portal's pg adapter
--     uses this role.
--   - polaris_anon — NO access. Magic-link issuance is the portal layer's
--     responsibility, not anything a tenant-scoped request should touch.
--
-- NextAuth contract:
--   - identifier: TEXT — the email address the magic link was sent to
--   - token:      TEXT — sha256-hashed token (NextAuth hashes before storage)
--   - expires:    TIMESTAMPTZ — 15-min default per MAGIC_LINK_EXPIRY_MIN
--   PK = (identifier, token) so the same email can have multiple in-flight
--   tokens (browser tabs, resends) without collision. NextAuth deletes
--   the row on successful consume — a stale/expired row is harmless until
--   the GC sweep runs.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   DROP TABLE IF EXISTS magic_link_tokens;

CREATE TABLE magic_link_tokens (
  identifier  TEXT         NOT NULL,
  token       TEXT         NOT NULL,
  expires     TIMESTAMPTZ  NOT NULL,
  PRIMARY KEY (identifier, token)
);

-- Used by the GC sweep + by the adapter's useVerificationToken DELETE.
-- The PK already indexes (identifier, token) ASC, so DELETE WHERE
-- identifier=$1 AND token=$2 is index-only. A secondary index on
-- expires lets the GC scan stale rows cheaply.
CREATE INDEX magic_link_tokens_expires_idx ON magic_link_tokens (expires);

-- NextAuth identifies users by email; the adapter normalises to lowercase
-- before insert/lookup so case-mismatches don't fragment tokens. Adapter-
-- side normalisation (not a DB CITEXT column) so the contract stays
-- driver-agnostic.

-- No RLS: this table is not tenant-scoped. Magic-link issuance happens
-- BEFORE a tenant context exists (the whole point of the login flow
-- is to establish one). polaris_anon has zero need to read this table,
-- so we grant nothing there.
GRANT ALL ON magic_link_tokens TO polaris_service;

COMMENT ON TABLE magic_link_tokens IS
  'Phase 8 PR-2 — NextAuth EmailProvider verification token store. Custom minimal adapter (apps/portal/lib/auth/adapter.ts) reads/writes this; no next_auth.* schema is used. Rows TTL via expires column (15-min default).';

INSERT INTO schema_migrations (filename) VALUES ('052_phase8_magic_link_tokens.sql')
ON CONFLICT (filename) DO NOTHING;
