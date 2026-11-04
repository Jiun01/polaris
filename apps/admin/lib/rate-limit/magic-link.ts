/**
 * Admin magic-link rate limit (Phase 9 PR-2).
 *
 * Mirrors apps/portal/lib/rate-limit/magic-link.ts but with a stricter
 * cap per ADR-030 Amendment 2026-05-20: 3 requests/hour (vs portal's 5)
 * per email. Defence-in-depth: protects the upstream SMTP server from
 * a malicious actor spraying signin requests at the operator's known
 * admin email, AND limits how often a leaked admin email can be used
 * to flood the operator's inbox with magic-link mail.
 *
 * In-memory LRU at GA (single-process admin container). Swap-out to
 * Redis becomes a single-file change if admin ever scales horizontally.
 */

import { LRUCache } from "lru-cache";

import { requestsPerHour } from "@/lib/auth/config";

const WINDOW_MS = 60 * 60 * 1_000; // 1 hour
const MAX_TRACKED_EMAILS = 1_000; // bound memory; LRU evicts oldest

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
 * the per-email cap (currently 3/hour by default per ADR-030 Am.2026-05-20).
 * Increments the counter on allow.
 */
export function allowMagicLink(email: string): boolean {
  const key = email.trim().toLowerCase();
  if (!key) return false;

  const cap = requestsPerHour();
  const now = Date.now();
  const existing = cache.get(key);

  if (existing === undefined || now - existing.windowStart >= WINDOW_MS) {
    cache.set(key, { count: 1, windowStart: now });
    return true;
  }

  if (existing.count >= cap) {
    return false;
  }

  existing.count += 1;
  cache.set(key, existing);
  return true;
}

/** Test-only: clear all counters. */
export function _resetForTests(): void {
  cache.clear();
}

export const _config = {
  WINDOW_MS,
  MAX_TRACKED_EMAILS,
} as const;
