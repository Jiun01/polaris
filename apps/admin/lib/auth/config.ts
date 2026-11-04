/**
 * Shared admin auth env helpers (Phase 9 PR-2).
 *
 * Mirrors `apps/portal/lib/auth/config.ts` but reads admin-specific env
 * vars (ADMIN_MAGIC_LINK_EXPIRY_MIN, ADMIN_MAGIC_LINK_IP_BIND_PREFIX,
 * etc.) so a future tweak to admin's stricter expiry doesn't drift
 * between the email body, NextAuth options, the verify-request copy,
 * and the IP-bind middleware.
 *
 * Defaults match ADR-030 Amendment 2026-05-20 compensating controls:
 *   - 10-minute magic-link expiry  (portal default: 15)
 *   - 3 magic-link requests/hour   (portal default: 5)
 *   - /24 IP-bind prefix           (portal: not applicable)
 *   - 1-day session lifetime       (portal default: 30 days)
 */

const DEFAULT_EXPIRY_MIN = 10;
const DEFAULT_REQUESTS_PER_HOUR = 3;
const DEFAULT_IP_BIND_PREFIX = 24;
const DEFAULT_SESSION_MAX_AGE_S = 24 * 60 * 60; // 1 day

/** Magic-link expiry in MINUTES (clamped positive, default 10). */
export function expiryMinutes(): number {
  const raw = process.env.ADMIN_MAGIC_LINK_EXPIRY_MIN;
  if (!raw) return DEFAULT_EXPIRY_MIN;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_EXPIRY_MIN;
}

/** Magic-link expiry in SECONDS — used by NextAuth EmailProvider.maxAge. */
export function expirySeconds(): number {
  return expiryMinutes() * 60;
}

/** Per-email throttle on POST /api/auth/signin/email (clamped positive). */
export function requestsPerHour(): number {
  const raw = process.env.ADMIN_MAGIC_LINK_REQUESTS_PER_HOUR;
  if (!raw) return DEFAULT_REQUESTS_PER_HOUR;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0
    ? parsed
    : DEFAULT_REQUESTS_PER_HOUR;
}

/**
 * IP-bind prefix length used by the session callback to compare the
 * click IP against the stashed sign-in-request IP. /24 by default
 * (tolerates same-LAN re-connect); /32 enforces exact-IP match.
 * Values outside 8..32 are clamped to the default.
 */
export function ipBindPrefix(): number {
  const raw = process.env.ADMIN_MAGIC_LINK_IP_BIND_PREFIX;
  if (!raw) return DEFAULT_IP_BIND_PREFIX;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed < 8 || parsed > 32) {
    return DEFAULT_IP_BIND_PREFIX;
  }
  return parsed;
}

/** NextAuth JWT session lifetime in seconds (default 86400 = 1 day). */
export function sessionMaxAgeSeconds(): number {
  const raw = process.env.ADMIN_SESSION_MAX_AGE_S;
  if (!raw) return DEFAULT_SESSION_MAX_AGE_S;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0
    ? parsed
    : DEFAULT_SESSION_MAX_AGE_S;
}

/**
 * Whether the deployment is HTTPS — drives NextAuth's secure-cookie
 * variant selection. Reads ADMIN_NEXTAUTH_URL because NODE_ENV alone
 * is insufficient (a staging URL on plain HTTP still has
 * NODE_ENV=production but must not use the secure-cookie variant).
 */
export function isSecureDeployment(): boolean {
  return process.env.ADMIN_NEXTAUTH_URL?.startsWith("https://") ?? false;
}
