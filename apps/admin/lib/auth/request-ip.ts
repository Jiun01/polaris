/**
 * Per-request IP propagation for the NextAuth jwt callback (Phase 9 PR-2).
 *
 * Problem: NextAuth v4's `jwt` callback signature doesn't include a
 * `request` argument (that's a v5 feature). To enforce the IP-bind
 * compensating control per ADR-030 Am.2026-05-20, the jwt callback
 * needs to know the CURRENT request's source IP — i.e. the click IP,
 * which it must compare against the recorded sign-in-request IP.
 *
 * Solution: Node's AsyncLocalStorage gives us a thread-equivalent
 * implicit context. The catch-all route handler
 * (apps/admin/app/api/auth/[...nextauth]/route.ts) extracts the IP
 * from the incoming Request's X-Forwarded-For + X-Real-IP, then runs
 * NextAuth inside an als.run() scope so the jwt callback can read it
 * back without dependency injection or threading the value through
 * NextAuth's option shape.
 *
 * Why not just key the IP-bind LRU by something the jwt callback
 * already has?
 *
 *   - The only stable identifier the jwt callback gets is `user.email`,
 *     which is the cache KEY for the bind. To CHECK the bind we need
 *     the *current request's* IP — i.e. a separate value, not in the
 *     cache and not derivable from the cache.
 *
 *   - Threading the IP through NextAuth's option shape would require
 *     building a fresh `authOptions` per request (so the IP is captured
 *     by closure). That fights NextAuth's intended init-once flow and
 *     adds a sharp-edged surface for the next reader.
 *
 * AsyncLocalStorage scope:
 *
 *   - The route handler's als.run() wraps the entire NextAuth invocation
 *     for one request. Every callback NextAuth fires synchronously or
 *     via awaited promises stays inside the same async context, so
 *     getRequestIpForJwt() inside any callback returns the right IP.
 *
 *   - Outside the als.run() scope, getRequestIpForJwt() returns null.
 *     The jwt callback treats that as a fail-closed condition (per
 *     options.ts) — no IP means no mint.
 */

import { AsyncLocalStorage } from "node:async_hooks";

interface RequestContext {
  /** Source IP of the current admin request, parsed from forwarded headers. */
  clickIp: string;
}

const als = new AsyncLocalStorage<RequestContext>();

/**
 * Run `fn` with the given click IP available to any descendant async
 * task via getRequestIpForJwt(). Called by the route handler before
 * delegating to NextAuth.
 */
export function withRequestIp<T>(
  clickIp: string,
  fn: () => Promise<T>,
): Promise<T> {
  return als.run({ clickIp }, fn);
}

/**
 * Read the click IP propagated from the active route-handler scope.
 * Returns null when called outside an `withRequestIp()` scope (which
 * shouldn't happen at runtime — that's a wiring bug; callers treat it
 * as fail-closed).
 */
export function getRequestIpForJwt(): string | null {
  const ctx = als.getStore();
  return ctx?.clickIp ?? null;
}

/**
 * Parse the source IP out of forwarded headers in the order Caddy +
 * Next 14 set them. Falls back to "" when nothing usable is present —
 * the caller (route handler) treats that as a fail-closed condition.
 *
 * Behind Caddy:
 *
 *   - X-Forwarded-For carries `<client>, <proxy1>, <proxy2>`.
 *     We take the first token (the original client).
 *
 *   - X-Real-IP is set by some reverse-proxy configs as a single value.
 *
 *   - At GA the admin app is single-proxy behind Caddy on the same host;
 *     X-Forwarded-For arrives as a single IP. Code is forward-compatible
 *     with a chain.
 *
 * IP-bind layer ONLY trusts these headers when the request also carries
 * the shared-secret header `X-Polaris-Caddy-Token` matching the value
 * in `ADMIN_CADDY_PROXY_TOKEN`. Caddy injects this header on every
 * upstream call; a client connecting directly to the Next.js process
 * (bypassing Caddy via misconfigured firewall or Docker exposure)
 * cannot forge the value because they don't know it. This is the
 * defence-in-depth check for security review Finding 1 — without it,
 * any direct-to-process spoof of X-Forwarded-For would pass the
 * IP-bind /24 check.
 *
 * If ADMIN_CADDY_PROXY_TOKEN is unset, we warn (once at module load)
 * and fall back to the no-check behaviour for dev. Production deploys
 * MUST set this env var; the warning is the operator's signal.
 */

const CADDY_TOKEN_HEADER = "x-polaris-caddy-token";

if (
  process.env.NODE_ENV === "production" &&
  !process.env.ADMIN_CADDY_PROXY_TOKEN
) {
  // eslint-disable-next-line no-console
  console.warn(
    "[admin request-ip] ADMIN_CADDY_PROXY_TOKEN is not set in production. " +
      "The IP-bind layer will trust X-Forwarded-For / X-Real-IP from any " +
      "source, which is unsafe if the Next.js process is reachable directly " +
      "(bypassing Caddy). Set this env var + matching Caddy header_up directive.",
  );
}

function caddyTokenMatches(req: Request): boolean {
  const expected = process.env.ADMIN_CADDY_PROXY_TOKEN;
  if (!expected) {
    // Dev-mode fallback: no token configured, accept anything. The
    // module-load warning above flagged this for production.
    return true;
  }
  return req.headers.get(CADDY_TOKEN_HEADER) === expected;
}

export function extractClickIp(req: Request): string {
  // Fail-closed if the request isn't coming through Caddy. Returning ""
  // makes the jwt callback reject with `missing click IP for bind check`
  // (per options.ts), which is the correct user-facing outcome —
  // they're told to retry from the same network.
  if (!caddyTokenMatches(req)) {
    return "";
  }
  const xff = req.headers.get("x-forwarded-for");
  if (xff) {
    const first = xff.split(",")[0]?.trim();
    if (first) return first;
  }
  const realIp = req.headers.get("x-real-ip");
  if (realIp) return realIp.trim();
  return "";
}
