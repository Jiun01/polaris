-- Migration 055 — Phase 9 PR-2 admin magic-link token storage.
-- Ref: docs/Polaris V2/04j_PHASE_9_DEEP.md §5.1 (admin NextAuth options) +
--      Q-PHASE9-01 (admin magic-link, drop MFA, IP-bind compensating control).
-- Anchors:
--   - ADR-030 (magic-link entry point).
--   - ADR-030 Amendment 2026-05-20 (drop platform_admin MFA at GA; 10-min
--     expiry + IP-bind /24 + 1-day session lifetime + Caddy basic-auth front).
--   - ADR-012 (sales-only signup — admin signIn callback rejects emails
--     without a corresponding platform_admins row, mirroring portal's
--     tenant_users gate).
--
-- Mirrors migration 052 (portal's magic_link_tokens) exactly. We keep the
-- admin tokens in a SEPARATE table so:
--
--   (a) Portal-side magic-link issuance can never accidentally mint an
--       admin-scoped link, and vice versa. The two tables are disjoint at
--       the storage layer, not just at the email-domain layer.
--
--   (b) A bug in one app's adapter cannot consume the other's tokens. A
--       wildcard DELETE in portal code that somehow scoped wrong cannot
--       touch admin_magic_link_tokens.
--
--   (c) Auditing is easier — a row in admin_magic_link_tokens is
--       unambiguously an admin sign-in attempt.
--
-- NextAuth v4 EmailProvider stores one-time verification tokens here. The
-- admin app's custom adapter (apps/admin/lib/auth/adapter.ts) uses ONLY
-- this table for token CRUD + JOINs platform_admins for the user lookup.
-- We do NOT use NextAuth's full schema (users/accounts/sessions) because:
--
--   - "Is this email a valid platform_admin?" is answered by
--     platform_admins (migration 054). Duplicating that into NextAuth's
--     own users table would create a drift surface.
--
--   - JWT session strategy means NextAuth never touches its `sessions`
--     table — JWT lives entirely in the browser cookie (1-day TTL per
--     ADR-030 Am.2026-05-20).
--
--   - `accounts` is for OAuth providers; we ship magic-link only at GA.
--
-- One table is enough.
--
-- Access:
--   - polaris_service (BYPASSRLS) — full CRUD; the admin app's pg adapter
--     uses this role.
--   - polaris_anon — explicitly REVOKEd. Admin magic-link issuance must
--     never be reachable from tenant context.
--
-- NextAuth contract:
--   - identifier: TEXT — the email address the magic link was sent to
--   - token:      TEXT — sha256-hashed token (NextAuth hashes before storage)
--   - expires:    TIMESTAMPTZ — 10-min per ADMIN_MAGIC_LINK_EXPIRY_MIN
--   PK = (identifier, token) so the same email can have multiple in-flight
--   tokens (browser tabs, resends) without collision. NextAuth deletes
--   the row on successful consume — a stale/expired row is harmless until
--   the GC sweep runs.
--
-- Rollback (local dev only; prod uses Supabase PITR):
--   DROP TABLE IF EXISTS admin_magic_link_tokens;

CREATE TABLE admin_magic_link_tokens (
  identifier  TEXT         NOT NULL,
  token       TEXT         NOT NULL,
  expires     TIMESTAMPTZ  NOT NULL,
  PRIMARY KEY (identifier, token)
);

-- Used by the GC sweep + by the adapter's useVerificationToken DELETE.
-- The PK already indexes (identifier, token) ASC, so DELETE WHERE
-- identifier=$1 AND token=$2 is index-only. A secondary index on
-- expires lets the GC scan stale rows cheaply.
CREATE INDEX admin_magic_link_tokens_expires_idx ON admin_magic_link_tokens (expires);

-- Adapter-side normalisation (lowercase identifier before insert/lookup)
-- mirrors the portal pattern, keeping the contract driver-agnostic.

-- No RLS: not tenant-scoped. Magic-link issuance happens BEFORE a session
-- exists — there is no tenant or platform_admin context to scope against.
-- polaris_anon has zero need to read this table, so explicitly REVOKE.
GRANT ALL ON admin_magic_link_tokens TO polaris_service;
REVOKE ALL ON admin_magic_link_tokens FROM polaris_anon;

COMMENT ON TABLE admin_magic_link_tokens IS
  'Phase 9 PR-2 — NextAuth EmailProvider verification-token store for admin.getpolaris.my. Custom minimal adapter (apps/admin/lib/auth/adapter.ts) reads/writes this; no next_auth.* schema is used. Rows TTL via expires column (10-min default per ADR-030 Am.2026-05-20).';

INSERT INTO schema_migrations (filename) VALUES ('055_phase9_admin_magic_link_tokens.sql')
ON CONFLICT (filename) DO NOTHING;
