/**
 * Portal-side Postgres pool (Phase 8 PR-2).
 *
 * Used by:
 *   - `lib/auth/adapter.ts`        — NextAuth verification-token CRUD +
 *                                    auth.users JOIN tenant_users lookup
 *   - `lib/auth/options.ts`        — signIn / jwt callbacks
 *   - Server-side route handlers   — server-action data fetches that
 *                                    can't go through the public API
 *                                    (rare; portal mostly calls Polaris API)
 *
 * The portal uses the `polaris_service` (BYPASSRLS) role for these
 * NextAuth-time queries because:
 *
 *   1. No tenant context exists during the auth handshake. RLS can't help
 *      yet — we need to look up `auth.users` + `tenant_users` by email
 *      BEFORE knowing which tenant the user belongs to.
 *
 *   2. The `magic_link_tokens` table is platform-scoped, not tenant-scoped.
 *      RLS would have nothing to gate.
 *
 *   3. The portal NEVER calls polaris_anon — RLS-gated DB reads are the
 *      API's job, not the portal's. Portal → API is the only data path
 *      after login.
 *
 * Connection string: POLARIS_DB_URL_SERVICE (same env var the API uses;
 * documented in the root .env.example). Pooling shape mirrors the API
 * defaults — max 10 connections per portal process is enough for the
 * NextAuth handshake plus any incidental server-side reads.
 */

import { Pool, type QueryResult, type QueryResultRow } from "pg";

let _pool: Pool | null = null;

/**
 * Return the singleton portal pg pool. Lazily constructed so that
 * `next build` doesn't fail when env vars are absent at build time.
 */
export function getPortalPool(): Pool {
  if (_pool !== null) return _pool;

  const rawUrl = process.env.POLARIS_DB_URL_SERVICE;
  if (!rawUrl) {
    throw new Error(
      "POLARIS_DB_URL_SERVICE is not set. The portal needs this for " +
        "NextAuth's magic-link adapter — copy it from the API's env.",
    );
  }

  // pg-connection-string >= 2.7 maps sslmode=require → verify-full, which
  // causes SELF_SIGNED_CERT_IN_CHAIN against Supabase's pooler. Strip the
  // sslmode param then inject ssl: { rejectUnauthorized: false } so pg
  // encrypts but skips Supabase's self-signed-root cert verification.
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
    max: 10,
    // Supabase's pooler closes idle connections after ~10 min; we time
    // out our side slightly under that so we don't try to send on a
    // half-dead socket.
    idleTimeoutMillis: 9 * 60 * 1000,
    // 5 s connection acquisition cap — if we can't get a connection in
    // that window during the magic-link handshake, something is wrong
    // upstream (pooler down) and we want a clear failure.
    connectionTimeoutMillis: 5_000,
    // TCP keep-alive so a half-dead socket (e.g. Supabase recycled the backend)
    // is detected and evicted rather than handed to a query that then hangs
    // until connectionTimeoutMillis.
    keepAlive: true,
  });

  // Capture the host once at pool creation so backend-error logs carry
  // enough context to identify which upstream the failure belongs to
  // (becomes load-bearing if the portal ever fans out to multiple
  // pools — e.g. a read-replica path).
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
    console.error(`[portal pg pool] backend error (host=${host})`, err);
  });

  return _pool;
}

/**
 * Raised when the portal's DB is unreachable (refused / timed out / circuit
 * open) as opposed to a genuine "no such row" result. Callers distinguish this
 * to degrade gracefully — the (portal) layout shows a transient "try again"
 * page instead of bouncing the user to /login in a loop, and auth paths fail
 * fast instead of hanging the magic-link handshake.
 */
export class DbUnavailableError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "DbUnavailableError";
  }
}

// pg surfaces socket errors via `err.code` (Node errno strings) and Postgres
// server errors via SQLSTATE. These are the codes that mean "couldn't reach /
// stay connected to the backend" — the symptom of a Supabase network ban
// (ECONNREFUSED), a paused project, or a recycled backend. A bad password
// (28P01) is deliberately NOT here: that's a config error to surface loudly,
// not retry behind a breaker.
const _TRANSIENT_CONN_CODES = new Set<string>([
  "ECONNREFUSED",
  "ETIMEDOUT",
  "EHOSTUNREACH",
  "ENETUNREACH",
  "ECONNRESET",
  "EPIPE",
  "ENOTFOUND",
  "08000", // connection_exception
  "08003", // connection_does_not_exist
  "08006", // connection_failure
  "08001", // sqlclient_unable_to_establish_sqlconnection
  "08004", // sqlserver_rejected_establishment_of_sqlconnection
  "57P01", // admin_shutdown
  "57P03", // cannot_connect_now
  "53300", // too_many_connections
]);

export function isTransientConnectionError(err: unknown): boolean {
  if (err instanceof DbUnavailableError) return true;
  const code = (err as { code?: unknown } | null)?.code;
  return typeof code === "string" && _TRANSIENT_CONN_CODES.has(code);
}

// ── Circuit breaker ──────────────────────────────────────────────────────
// Once the DB starts refusing connections (the Supabase network-ban signature
// is ECONNREFUSED on every new socket), retrying in a tight loop across every
// inbound request is exactly what SUSTAINS the ban — each attempt is another
// connection Supabase's firewall sees. After N consecutive transient failures
// we trip the breaker and fail fast for a cooldown WITHOUT opening new sockets,
// so the portal stops amplifying the outage. State is per-process (each Next
// server instance has its own), which is the right granularity here.
const _CB_FAILURE_THRESHOLD = 5;
const _CB_COOLDOWN_MS = 20_000;
const _CB_QUERY_RETRIES = 1; // one short in-request retry for a one-off blip
const _CB_RETRY_BACKOFF_MS = 250;

let _cbConsecutiveFailures = 0;
let _cbOpenUntil = 0;

/**
 * Run a query through the portal pool with bounded retry + a circuit breaker.
 *
 * - A genuine query/constraint error (bad SQL, unique violation, auth failure)
 *   propagates unchanged — those are bugs/config, not transient.
 * - A transient *connection* error gets ONE quick retry, then (if still
 *   failing) is rethrown as `DbUnavailableError` and counts toward the breaker.
 * - While the breaker is open, calls fail fast with `DbUnavailableError` and
 *   never touch a socket.
 *
 * Use this for every auth-time DB read so the breaker sees the full failure
 * stream and the whole portal degrades coherently.
 */
export async function portalQuery<T extends QueryResultRow = QueryResultRow>(
  text: string,
  params?: ReadonlyArray<unknown>,
): Promise<QueryResult<T>> {
  if (Date.now() < _cbOpenUntil) {
    throw new DbUnavailableError(
      "portal DB circuit open — recent connections to Supabase are being refused " +
        "(possible network ban / paused project); failing fast to avoid a reconnect flood",
    );
  }

  const values = params ? ([...params] as unknown[]) : undefined;
  let lastErr: unknown;
  for (let attempt = 0; attempt <= _CB_QUERY_RETRIES; attempt += 1) {
    try {
      const res = await getPortalPool().query<T>(text, values);
      _cbConsecutiveFailures = 0; // success resets the streak
      return res;
    } catch (err) {
      lastErr = err;
      if (!isTransientConnectionError(err)) throw err;
      if (attempt < _CB_QUERY_RETRIES) {
        await new Promise((resolve) =>
          setTimeout(resolve, _CB_RETRY_BACKOFF_MS),
        );
      }
    }
  }

  _cbConsecutiveFailures += 1;
  if (_cbConsecutiveFailures >= _CB_FAILURE_THRESHOLD) {
    _cbOpenUntil = Date.now() + _CB_COOLDOWN_MS;
    _cbConsecutiveFailures = 0;
    // eslint-disable-next-line no-console
    console.error(
      `[portal pg] circuit OPEN for ${_CB_COOLDOWN_MS}ms after ${_CB_FAILURE_THRESHOLD} ` +
        "consecutive connection failures — failing fast to avoid amplifying a Supabase ban",
    );
  }
  throw new DbUnavailableError("portal DB unreachable", { cause: lastErr });
}
