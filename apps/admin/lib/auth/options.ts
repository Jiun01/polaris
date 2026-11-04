/**
 * NextAuth v4 configuration for the Polaris admin (Phase 9 PR-2).
 *
 * Mirrors apps/portal/lib/auth/options.ts but with admin-specific:
 *
 *   - Custom adapter targets platform_admins (migration 054) +
 *     admin_magic_link_tokens (migration 055).
 *   - signIn callback rejects emails not in platform_admins (active=TRUE).
 *   - jwt callback ENFORCES IP-bind: the click IP must share /24 with
 *     the recorded sign-in-request IP per ADR-030 Am.2026-05-20.
 *     Throwing here lets NextAuth catch the error and surface a clean
 *     redirect to /api/auth/error?error=AccessDenied.
 *   - Only one custom claim: platform_admin_id. Admin operates above
 *     tenants; there is no tenant_id / plan_tier / suspended_reason.
 *
 * Anchors:
 *   - ADR-012 (sales-only signup)
 *   - ADR-030 (magic-link entry)
 *   - ADR-030 Amendment 2026-05-20 (drop platform_admin MFA at GA;
 *     compensating controls = Caddy basic-auth, 10-min expiry,
 *     IP-bind, 1-day session)
 *   - 04j §5.1 (NextAuth options shape) + §11 Q-PHASE9-01
 */

import type { NextAuthOptions } from "next-auth";
import EmailProvider from "next-auth/providers/email";

import { AdminAdapter } from "@/lib/auth/adapter";
import { expirySeconds, sessionMaxAgeSeconds } from "@/lib/auth/config";
import { getAdminPool } from "@/lib/auth/db";
import { sendVerificationRequest } from "@/lib/auth/email-template";
import { checkBindForEmail } from "@/lib/auth/ip-bind";
import { getRequestIpForJwt } from "@/lib/auth/request-ip";

interface ResolvedAdmin {
  platform_admin_id: string;
  email: string;
}

async function resolveActivePlatformAdmin(
  email: string,
): Promise<ResolvedAdmin | null> {
  const normalized = email.trim().toLowerCase();
  const { rows } = await getAdminPool().query<{
    platform_admin_id: string;
    email: string;
  }>(
    `
    SELECT id AS platform_admin_id, email
      FROM platform_admins
     WHERE lower(email) = $1
       AND active = TRUE
     LIMIT 1
    `,
    [normalized],
  );
  if (rows.length === 0) return null;
  return rows[0];
}

// ADMIN_EMAIL_SERVER_USER and ADMIN_EMAIL_SERVER_PASSWORD are intentionally
// excluded — they are optional for unauthenticated SMTP relays (e.g. MailHog
// in local dev / VPS-mimic mode) and must be allowed to be empty strings.
const REQUIRED_AUTH_ENV = [
  "ADMIN_NEXTAUTH_SECRET",
  "ADMIN_EMAIL_SERVER_HOST",
] as const;

let _missingAuthEnv: readonly string[] | null = null;

/**
 * Lazy env validation. NOT called at module load — `next build`'s
 * page-data collection evaluates this module without runtime env vars
 * set. The route handler invokes this on the first incoming request.
 *
 * Returns the list of missing required env vars (empty when all present).
 * Logs once on first computation. The route handler short-circuits to a
 * clean `server_misconfigured` 500 when this is non-empty, instead of
 * letting the request proceed into an opaque nodemailer/JWT failure.
 */
export function validateAuthEnv(): readonly string[] {
  if (_missingAuthEnv === null) {
    _missingAuthEnv = REQUIRED_AUTH_ENV.filter((name) => !process.env[name]);
    if (_missingAuthEnv.length > 0) {
      // eslint-disable-next-line no-console
      console.error(
        `[admin NextAuth] missing required env vars: ${_missingAuthEnv.join(", ")}. ` +
          "Sign-in will fail until these are set. See apps/admin/.env.example.",
      );
    }
  }
  return _missingAuthEnv;
}

/**
 * Promote ADMIN_NEXTAUTH_* env vars onto the names NextAuth's library
 * code reads directly (NEXTAUTH_URL, NEXTAUTH_SECRET). NextAuth v4 has
 * hard-coded reads of those names inside getToken/buildUrl helpers; we
 * keep the admin-prefixed names as the public surface (distinct from
 * portal's) but bridge at runtime.
 *
 * Called from the route handler before NextAuth is invoked. Safe to
 * call repeatedly — ADMIN_NEXTAUTH_URL always wins over NEXTAUTH_URL
 * because the root .env sets NEXTAUTH_URL=http://localhost:3000 (portal)
 * which would otherwise leak into the admin container via env_file.
 */
export function syncAuthEnvAliases(): void {
  // Always override — do NOT guard with `=== undefined`.
  // The root .env sets NEXTAUTH_URL for the portal; admin containers inherit
  // it via docker-compose env_file and must stomp it with the admin URL.
  if (process.env.ADMIN_NEXTAUTH_URL) {
    process.env.NEXTAUTH_URL = process.env.ADMIN_NEXTAUTH_URL;
  }
  if (process.env.ADMIN_NEXTAUTH_SECRET) {
    process.env.NEXTAUTH_SECRET = process.env.ADMIN_NEXTAUTH_SECRET;
  }
}

export const authOptions: NextAuthOptions = {
  adapter: AdminAdapter(),

  // Explicit secret so NextAuth signs and verifies with ADMIN_NEXTAUTH_SECRET
  // regardless of what NEXTAUTH_SECRET is set to in the container env.
  // The shared root .env sets NEXTAUTH_SECRET to the portal secret; without
  // this explicit override NextAuth would sign admin JWTs with the portal
  // secret, while the middleware (Edge Runtime) verifies with ADMIN_NEXTAUTH_SECRET
  // — causing every session to appear invalid.
  secret: process.env.ADMIN_NEXTAUTH_SECRET,

  session: {
    strategy: "jwt",
    maxAge: sessionMaxAgeSeconds(),
  },

  pages: {
    signIn: "/login",
    verifyRequest: "/verify-request",
    // No custom error page at GA — NextAuth's default suffices.
  },

  providers: [
    EmailProvider({
      server: {
        host: process.env.ADMIN_EMAIL_SERVER_HOST ?? "",
        port: Number.parseInt(process.env.ADMIN_EMAIL_SERVER_PORT ?? "587", 10),
        auth: {
          user: process.env.ADMIN_EMAIL_SERVER_USER ?? "",
          pass: process.env.ADMIN_EMAIL_SERVER_PASSWORD ?? "",
        },
      },
      from:
        process.env.ADMIN_EMAIL_FROM ??
        "Polaris Admin <no-reply@getpolaris.my>",
      maxAge: expirySeconds(),
      sendVerificationRequest,
    }),
  ],

  callbacks: {
    /**
     * Hard gate. Returning `false` blocks sign-in; user lands on the
     * error page with `?error=AccessDenied`. We accept only emails that
     * resolve to an ACTIVE platform_admins row. The adapter's
     * getUserByEmail already enforces this (returning null), so this
     * callback is belt-and-braces — under the AdapterUser path,
     * `user.email` is set only when the adapter found a row.
     *
     * Fail-closed on DB error: any exception from the adapter chain
     * propagates here; we explicitly reject rather than allow through.
     */
    async signIn({ user }): Promise<boolean> {
      if (!user.email) return false;
      try {
        const resolved = await resolveActivePlatformAdmin(user.email);
        return resolved !== null;
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error(
          "[admin signIn] resolveActivePlatformAdmin failed; rejecting (fail-closed).",
          err,
        );
        return false;
      }
    },

    /**
     * Mint the JWT. Two responsibilities here that the portal's jwt
     * callback doesn't have:
     *
     *   1. IP-bind enforcement. The click's source IP must share /24
     *      with the recorded sign-in-request IP (compensating control
     *      per ADR-030 Am.2026-05-20). The route handler stashed the
     *      request IP into the in-memory LRU before delegating; this
     *      callback reads it back via checkBindForEmail.
     *
     *   2. Resolve platform_admin_id from email — analogous to portal's
     *      tenant resolution, but to platform_admins instead.
     *
     * Throwing here is the correct exit path: NextAuth v4 catches
     * jwt-callback errors and surfaces a clean error-page redirect.
     * NEVER do this in the session callback — that crashes 500.
     */
    async jwt({ token, user }) {
      if (!user?.email) {
        return token;
      }
      // IP-bind check fires on the click. The click IP was stashed by
      // the route handler (request-ip.ts) into a per-request async
      // local so callbacks can read it without dependency injection.
      const clickIp = getRequestIpForJwt();
      if (!clickIp) {
        // We were not given a click IP — defensive: refuse. Indicates
        // the route handler wiring is broken; loud log so the operator
        // notices.
        // eslint-disable-next-line no-console
        console.error(
          "[admin jwt] no click IP available; refusing to mint token (fail-closed).",
        );
        throw new Error("admin jwt: missing click IP for bind check");
      }
      if (!checkBindForEmail(user.email, clickIp)) {
        // eslint-disable-next-line no-console
        console.warn(
          `[admin jwt] IP-bind mismatch for ${user.email}; refusing to mint token.`,
        );
        throw new Error("admin jwt: IP-bind mismatch");
      }

      let resolved: ResolvedAdmin | null;
      try {
        resolved = await resolveActivePlatformAdmin(user.email);
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error(
          "[admin jwt] resolveActivePlatformAdmin failed; refusing mint (fail-closed).",
          err,
        );
        throw err;
      }
      if (resolved === null) {
        // eslint-disable-next-line no-console
        console.error(
          `[admin jwt] email ${user.email} resolved no active platform_admin; ` +
            "refusing to mint token. Check that platform_admins has an active " +
            "row for this email. Run: uv run python apps/api/scripts/seed_e2e_admin.py",
        );
        throw new Error(
          `[admin jwt] email ${user.email} resolved no active platform_admin`,
        );
      }
      token.platform_admin_id = resolved.platform_admin_id;
      return token;
    },

    /**
     * Surface the platform_admin_id claim to the client / server
     * components. Module augmentation in types/next-auth.d.ts makes
     * this typesafe.
     */
    async session({ session, token }) {
      session.user.id = token.platform_admin_id ?? "";
      session.platform_admin_id = token.platform_admin_id;
      return session;
    },
  },
};
