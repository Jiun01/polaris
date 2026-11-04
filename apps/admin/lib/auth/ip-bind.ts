/**
 * IP-bind compensating control for admin magic-link (Phase 9 PR-2).
 *
 * Background: ADR-030 Amendment 2026-05-20 drops mandatory MFA for
 * `platform_admin` at GA. To compensate, the admin magic-link flow
 * stashes the source IP of the sign-in REQUEST (the email-entry POST
 * to /api/auth/signin/email) and, when the user later clicks the
 * emailed link, the `jwt` callback rejects the click if the click's
 * source IP is not in the same /24 (configurable via
 * ADMIN_MAGIC_LINK_IP_BIND_PREFIX) as the request's source IP.
 *
 * Threat model this addresses:
 *
 *   - Email-in-transit interception by a passive eavesdropper on the
 *     user's network. Even if the attacker gets the magic link, they
 *     can only consume it from the same /24, which is the same
 *     network they're already on for the interception to be possible
 *     in the first place — so this primarily defends against link
 *     forwarding (accidental or coerced) to a third party off-network.
 *
 *   - Phishing where a user is tricked into pasting a magic-link
 *     URL into an attacker-controlled context. The attacker's egress
 *     IP won't match the operator's, so the click is rejected.
 *
 * Threat model this does NOT address:
 *
 *   - On-host browser compromise where the attacker shares the user's
 *     network. The user's existing session JWT (1-day max-age) is
 *     what gates that surface; IP-bind only fires on the click step.
 *
 *   - CGNAT / mobile carrier double-NAT scenarios where the request
 *     IP and click IP routinely differ. /24 default is permissive
 *     enough for most home/office networks; operators on mobile-only
 *     setups should override ADMIN_MAGIC_LINK_IP_BIND_PREFIX to a
 *     larger prefix (e.g. /16) or accept the trade-off.
 *
 * Why in-memory LRU and not the database:
 *
 *   - The bind is request-scoped: lifetime is at most one magic-link
 *     expiry window (10 min default). A row in admin_magic_link_tokens
 *     COULD carry the IP, but NextAuth's adapter contract doesn't give
 *     us a clean place to write it before token creation runs.
 *
 *   - Cross-process state isn't a problem at GA (single admin container).
 *     If we ever scale admin horizontally, this module is a single-
 *     file swap to Redis.
 *
 *   - LRU bounds memory automatically. We size for 1000 in-flight
 *     sign-in attempts — way over realistic load for a single-admin app.
 *
 * Why per-email keying and not per-token:
 *
 *   - The request handler runs BEFORE NextAuth mints the token, so
 *     we don't have a token to key on yet.
 *
 *   - Email is the only stable identifier across the request →
 *     verification token → click chain. NextAuth's `signIn` /
 *     `jwt` callbacks both receive the user's email.
 *
 *   - Per-email also means a re-request from the same email overwrites
 *     the prior bind, which is what we want (latest request wins).
 */

import { LRUCache } from "lru-cache";

import { expirySeconds, ipBindPrefix } from "@/lib/auth/config";

interface BindEntry {
  /** Source IP of the sign-in request, as observed at the route handler. */
  ip: string;
  /** Unix ms when the bind was recorded. Used for staleness audit only. */
  recordedAt: number;
}

const cache = new LRUCache<string, BindEntry>({
  max: 1_000,
  // TTL just over the magic-link expiry, so a click after expiry still
  // finds a bind entry (so the resulting failure path is "expired link"
  // surfaced by NextAuth, not a misleading "IP mismatch").
  ttl: (expirySeconds() + 60) * 1_000,
});

/** Normalise an email for cache keys: lowercase, trim. */
function normEmail(email: string): string {
  return email.trim().toLowerCase();
}

/**
 * Record the source IP for an incoming magic-link request. Called by the
 * `/api/auth/[...nextauth]/route.ts` handler BEFORE delegating to NextAuth,
 * so the bind is captured even if downstream steps throw.
 */
export function recordBindForRequest(email: string, ip: string): void {
  const key = normEmail(email);
  if (!key || !ip) return;
  cache.set(key, { ip, recordedAt: Date.now() });
}

/**
 * Look up the recorded bind for an email — used by the `jwt` callback
 * in `options.ts` when validating a click. Returns null when:
 *
 *   - No bind was recorded for this email (link was minted out-of-band,
 *     or our process restarted between request and click).
 *   - The TTL has expired (NextAuth would also reject the link as expired
 *     in this case via the adapter's row TTL).
 */
export function lookupBindForEmail(email: string): BindEntry | null {
  return cache.get(normEmail(email)) ?? null;
}

/**
 * IPv4 /N prefix comparison. Returns true when both addresses share the
 * top `prefix` bits (as parsed-octet bytes; /24 means top 24 bits = first
 * three octets match). IPv6 addresses match if string-equal at /32 or
 * mismatch otherwise (we don't ship dual-stack prefix math at GA; admin
 * deploys behind Caddy which serves IPv4 in the X-Forwarded-For chain at
 * GA infra).
 */
export function ipMatchesPrefix(a: string, b: string, prefix: number): boolean {
  if (!a || !b) return false;
  if (a === b) return true;
  if (prefix >= 32) return false;

  const aParts = a.split(".").map((p) => Number.parseInt(p, 10));
  const bParts = b.split(".").map((p) => Number.parseInt(p, 10));
  if (aParts.length !== 4 || bParts.length !== 4) return false;
  if (aParts.some((p) => Number.isNaN(p) || p < 0 || p > 255)) return false;
  if (bParts.some((p) => Number.isNaN(p) || p < 0 || p > 255)) return false;

  // Compose 32-bit ints. >>> 0 forces unsigned semantics.
  const aInt =
    ((aParts[0] << 24) | (aParts[1] << 16) | (aParts[2] << 8) | aParts[3]) >>>
    0;
  const bInt =
    ((bParts[0] << 24) | (bParts[1] << 16) | (bParts[2] << 8) | bParts[3]) >>>
    0;
  const mask = prefix === 0 ? 0 : (~0 << (32 - prefix)) >>> 0;
  return (aInt & mask) === (bInt & mask);
}

/**
 * Combined check used by the `jwt` callback. Returns true when the
 * click's IP matches the recorded request IP at the configured prefix.
 * Returns false on:
 *
 *   - No recorded bind (fail-closed for the IP-bind layer; NextAuth
 *     itself will then redirect via the throw-from-jwt path).
 *   - Prefix mismatch.
 */
export function checkBindForEmail(email: string, clickIp: string): boolean {
  const bind = lookupBindForEmail(email);
  if (bind === null) return false;
  return ipMatchesPrefix(bind.ip, clickIp, ipBindPrefix());
}

/** Test-only — reset state between cases. */
export function _resetForTests(): void {
  cache.clear();
}
