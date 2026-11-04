/**
 * Magic-link rate limit (Phase 8 PR-2).
 *
 * In-memory LRU cap of 5 magic-link requests per hour per email.
 * Defence-in-depth: protects the upstream SMTP server from being
 * throttled by a malicious actor spraying signin requests at a known
 * tenant_admin email. The upstream provider (Resend / Brevo / whoever
 * Chatwoot reuses) has its own rate limits — we cap below those so
 * the legitimate user doesn't get locked out by an attack.
 *
 * Why in-memory and not Redis:
 *
 *   - The portal at GA is single-process (one Docker container per
 *     deploy; Next.js standalone). Cross-process state isn't a problem.
 *   - LRU bounds memory automatically — no grooming job, no stale-key
 *     leak.
 *   - Restart wipes the counters, which is fine: an attacker mid-spray
 *     who tries to capitalise on a restart still hits the upstream
 *     provider's rate limits.
 *
 * When the portal scales horizontally (post-GA), replace this module
 * with a Redis-backed token bucket against the existing REDIS_URL.
 * The exported function signature stays the same so the swap is a
 * single-file change.
 */

import { LRUCache } from "lru-cache";

const WINDOW_MS = 60 * 60 * 1_000; // 1 hour
const MAX_REQUESTS_PER_WINDOW = 5;
const MAX_TRACKED_EMAILS = 10_000; // bound memory; LRU evicts oldest

interface Counter {
  count: number;
  windowStart: number; // unix ms
}

const cache = new LRUCache<string, Counter>({
  max: MAX_TRACKED_EMAILS,
  ttl: WINDOW_MS,
});

/**
 * Returns `true` if the request should be allowed, `false` if it's over
 * the cap and should be rejected. Increments the counter on allow.
 *
 * Email is normalised (lower-cased + trimmed) so case variations of the
 * same address share a bucket.
 */
export function allowMagicLink(email: string): boolean {
  const key = email.trim().toLowerCase();
  if (!key) return false;

  const now = Date.now();
  const existing = cache.get(key);

  if (existing === undefined || now - existing.windowStart >= WINDOW_MS) {
    cache.set(key, { count: 1, windowStart: now });
    return true;
  }

  if (existing.count >= MAX_REQUESTS_PER_WINDOW) {
    return false;
  }

  existing.count += 1;
  // Re-set so the LRU's "recently-used" position updates; the existing
  // TTL is preserved by passing the same value back.
  cache.set(key, existing);
  return true;
}

/**
 * Test-only: clear all counters. Not exported from the package index;
 * tests reach in via the explicit lib/rate-limit/magic-link path.
 */
export function _resetForTests(): void {
  cache.clear();
}

export const _config = {
  WINDOW_MS,
  MAX_REQUESTS_PER_WINDOW,
  MAX_TRACKED_EMAILS,
} as const;
