/**
 * Shared auth env helpers (Phase 8 PR-2).
 *
 * Centralises the few env-var parsers that more than one auth file
 * reads. Keeps the default values + clamping logic in one place so a
 * future tweak to (e.g.) magic-link expiry doesn't drift between the
 * email body, NextAuth options, and the verify-request copy.
 */

const DEFAULT_EXPIRY_MIN = 15;
const DEFAULT_SESSION_MAX_AGE_S = 30 * 24 * 60 * 60;

/** Magic-link expiry in MINUTES (clamped positive, default 15). */
export function expiryMinutes(): number {
  const raw = process.env.MAGIC_LINK_EXPIRY_MIN;
  if (!raw) return DEFAULT_EXPIRY_MIN;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_EXPIRY_MIN;
}

/** Magic-link expiry in SECONDS — used by NextAuth EmailProvider.maxAge. */
export function expirySeconds(): number {
  return expiryMinutes() * 60;
}

/** NextAuth JWT session lifetime in seconds (default 30 days). */
export function sessionMaxAgeSeconds(): number {
  const raw = process.env.PORTAL_SESSION_MAX_AGE_S;
  if (!raw) return DEFAULT_SESSION_MAX_AGE_S;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0
    ? parsed
    : DEFAULT_SESSION_MAX_AGE_S;
}

/**
 * Whether the deployment is HTTPS — drives NextAuth's secure-cookie
 * variant selection (`__Secure-next-auth.session-token` vs the
 * non-prefixed variant). Read NEXTAUTH_URL because NODE_ENV='production'
 * alone is insufficient — a staging URL on plain HTTP would still set
 * NODE_ENV=production but must NOT use the secure-cookie variant.
 */
export function isSecureDeployment(): boolean {
  return process.env.NEXTAUTH_URL?.startsWith("https://") ?? false;
}
