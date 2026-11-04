/**
 * Admin-side Postgres pool (Phase 9 PR-2).
 *
 * Used by:
 *   - `lib/auth/adapter.ts`        — NextAuth verification-token CRUD on
 *                                    admin_magic_link_tokens (migration 055)
 *                                    + platform_admins lookup (migration 054)
 *   - `lib/auth/options.ts`        — signIn / jwt callbacks
 *
 * Mirrors `apps/portal/lib/auth/db.ts` exactly — same pool shape, same
 * env var (POLARIS_DB_URL_SERVICE), same timeouts. The admin process is
 * lower-volume than the portal but the pool semantics are identical.
 *
 * Uses the `polaris_service` (BYPASSRLS) role because:
 *
 *   1. No platform_admin context exists during the auth handshake. RLS
 *      can't help yet — we need to look up platform_admins by email
 *      BEFORE knowing which admin (if any) the email belongs to.
 *
 *   2. `admin_magic_link_tokens` is platform-scoped, not tenant-scoped.
 *      RLS would have nothing to gate.
 *
 *   3. `platform_admins` is explicitly service-pool-only per migration 054
 *      (polaris_anon REVOKEd). The admin app never calls polaris_anon —
 *      RLS-gated DB reads are the API's job.
 */

import { Pool } from "pg";

let _pool: Pool | null = null;

/**
 * Return the singleton admin pg pool. Lazily constructed so that
 * `next build` doesn't fail when env vars are absent at build time.
 */
export function getAdminPool(): Pool {
  if (_pool !== null) return _pool;

  const rawUrl = process.env.POLARIS_DB_URL_SERVICE;
  if (!rawUrl) {
    throw new Error(
      "POLARIS_DB_URL_SERVICE is not set. The admin app needs this for " +
        "NextAuth's magic-link adapter and platform_admins lookup — copy " +
        "it from the API's env. Same value the portal reads.",
    );
  }

  // pg-connection-string >= 2.7 maps sslmode=require → verify-full, which
  // causes SELF_SIGNED_CERT_IN_CHAIN against Supabase's pooler. Strip the
  // sslmode param from the URL so pg-connection-string never applies its own
  // ssl config, then inject ssl: { rejectUnauthorized: false } directly so
  // pg encrypts but skips Supabase's self-signed-root cert verification.
  let url = rawUrl;
  try {
    const u = new URL(rawUrl);
    u.searchParams.delete("sslmode");
    url = u.toString();
  } catch {
    // If the URL is unparseable, fall back to rawUrl and let pg fail clearly.
  }

  _pool = new Pool({
    connectionString: url,
    ssl: { rejectUnauthorized: false },
    // Admin is single-operator at GA; 5 connections is plenty. Portal
    // uses 10 because of higher volume. If we ever scale to a multi-
    // operator admin, revisit.
    max: 5,
    idleTimeoutMillis: 9 * 60 * 1000,
    connectionTimeoutMillis: 5_000,
  });

  let host = "?";
  try {
    host = new URL(url).hostname;
  } catch {
    // URL parse fails on bare strings; default already set.
  }

  _pool.on("error", (err) => {
    // Backend connection errors must be logged or the process can crash
    // silently on idle-connection failures. We can't surface this to a
    // user response — it fires on idle sockets, not active queries.
    // eslint-disable-next-line no-console
    console.error(`[admin pg pool] backend error (host=${host})`, err);
  });

  return _pool;
}
