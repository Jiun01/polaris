/**
 * NextAuth v4 configuration for the Polaris portal (Phase 8 PR-2).
 *
 * Decisions baked in (locked in 04i §11):
 *
 *   - EmailProvider only — magic-link, no password (ADR-030).
 *   - SMTP creds reused from Chatwoot's mailer (Q-PHASE8-01).
 *   - JWT session strategy — middleware reads the token on every
 *     request; a DB-backed session would force a Postgres round-trip
 *     per navigation, which we explicitly don't want.
 *   - signIn callback rejects unknown emails — public signup is gated
 *     by tenant_users provisioning (ADR-012).
 *   - JWT carries `user_id` (auth.users.id) + `tenant_id` +
 *     `tenant_slug` + `role` + `plan_tier` + `suspended_reason`. The
 *     middleware uses `suspended_reason` to fire the payment-overdue
 *     route-takeover (ADR-049); the API uses `tenant_id` + `user_id`
 *     to bind RLS via the existing TenantContext dep.
 *
 * Security boundaries:
 *
 *   - Magic-link replay: NextAuth's `useVerificationToken` (our adapter)
 *     deletes the row atomically on consume. A second click hits a
 *     "not found" row and bounces to the error page.
 *   - Tenant spoof: signIn rejects emails not in tenant_users. JWT
 *     callback re-resolves tenant_id on every issuance (not just
 *     first signin) so an admin who later revokes a tenant_users row
 *     can't continue using a stale JWT past its 30-day max.
 *   - Open redirect: NextAuth's default `redirect` callback rejects
 *     absolute URLs that aren't on the configured `NEXTAUTH_URL` host.
 *     We keep the default behaviour.
 *   - Fail-closed on DB outage during JWT mint: a DB error in the
 *     jwt/signIn callbacks must reject the sign-in attempt rather
 *     than issuing a token with stale or empty tenant claims (would
 *     bypass the suspension check in middleware).
 *
 * Anchors:
 *   - ADR-012, ADR-030, ADR-049, ADR-052
 *   - 04i §5.1, §5.4 (callbacks ↔ middleware contract)
 *   - 05 §4.1 (tenants columns including suspended_reason)
 */

import type { NextAuthOptions } from "next-auth";
import EmailProvider from "next-auth/providers/email";

import { PortalAdapter } from "@/lib/auth/adapter";
import { expirySeconds, sessionMaxAgeSeconds } from "@/lib/auth/config";
import { portalQuery } from "@/lib/auth/db";
import { sendVerificationRequest } from "@/lib/auth/email-template";

/**
 * Tenant context resolved server-side from the user's email at JWT-mint
 * time. Single SELECT per token mint — JWT is then cached in the cookie
 * for `PORTAL_SESSION_MAX_AGE_S` so this query doesn't fire per request.
 */
interface ResolvedTenant {
  user_id: string;
  tenant_id: string;
  tenant_slug: string;
  role: string;
  plan_tier: string | null;
  suspended_reason: string | null;
}

async function resolveTenantForEmail(
  email: string,
): Promise<ResolvedTenant | null> {
  const normalized = email.trim().toLowerCase();
  // Join via the SECURITY DEFINER helper (migration 066) to avoid direct
  // auth schema access which polaris_service cannot hold in Supabase.
  const { rows } = await portalQuery<ResolvedTenant>(
    `
    SELECT pu.id            AS user_id,
           t.id             AS tenant_id,
           t.slug           AS tenant_slug,
           tu.role          AS role,
           tq.plan_tier     AS plan_tier,
           t.suspended_reason AS suspended_reason
      FROM public.get_portal_user_by_email($1) pu
      JOIN tenant_users  tu ON tu.user_id = pu.id
      JOIN tenants       t  ON t.id = tu.tenant_id
 LEFT JOIN tenant_quotas tq ON tq.tenant_id = t.id
     LIMIT 1
    `,
    [normalized],
  );
  if (rows.length === 0) return null;
  return rows[0];
}

const REQUIRED_AUTH_ENV = [
  "NEXTAUTH_SECRET",
  "EMAIL_SERVER_HOST",
  "EMAIL_SERVER_USER",
  "EMAIL_SERVER_PASSWORD",
] as const;

let _missingAuthEnv: readonly string[] | null = null;

/**
 * Lazy env validation. NOT called at module load — `next build`'s
 * page-data collection evaluates this module without runtime env vars
 * set, so throwing at import would break the build. Instead, the
 * route handler invokes this on the first incoming request, by which
 * point the deploy environment has populated the vars.
 *
 * Returns the list of missing required env vars (empty when all present).
 * Logs once on first computation. The route handler short-circuits to a
 * clean `server_misconfigured` 500 when this is non-empty, instead of
 * letting the request fall into an opaque nodemailer/JWT failure.
 */
export function validateAuthEnv(): readonly string[] {
  if (_missingAuthEnv === null) {
    _missingAuthEnv = REQUIRED_AUTH_ENV.filter((name) => !process.env[name]);
    if (_missingAuthEnv.length > 0) {
      // eslint-disable-next-line no-console
      console.error(
        `[portal NextAuth] missing required env vars: ${_missingAuthEnv.join(", ")}. ` +
          "Sign-in will fail until these are set. See apps/portal/.env.example.",
      );
    }
  }
  return _missingAuthEnv;
}

export const authOptions: NextAuthOptions = {
  adapter: PortalAdapter(),

  session: {
    strategy: "jwt",
    maxAge: sessionMaxAgeSeconds(),
  },

  pages: {
    signIn: "/login",
    verifyRequest: "/verify-request",
    // No custom error page at GA — NextAuth's default error page
    // is fine and we don't want to maintain another route this PR.
  },

  providers: [
    EmailProvider({
      server: {
        host: process.env.EMAIL_SERVER_HOST ?? "",
        port: Number.parseInt(process.env.EMAIL_SERVER_PORT ?? "587", 10),
        auth: {
          user: process.env.EMAIL_SERVER_USER ?? "",
          pass: process.env.EMAIL_SERVER_PASSWORD ?? "",
        },
      },
      from: process.env.EMAIL_FROM ?? "Polaris <no-reply@getpolaris.my>",
      maxAge: expirySeconds(),
      sendVerificationRequest,
    }),
  ],

  callbacks: {
    /**
     * Hard gate. Returning `false` blocks the sign-in attempt; the
     * user lands on the error page with `?error=AccessDenied`. We
     * accept only emails that resolve to a tenant_users row (i.e.
     * were provisioned through the admin path).
     *
     * Fail-closed on DB error: any exception from
     * resolveTenantForEmail (pool timeout, transient network blip,
     * Supabase pooler restart) is caught + logged + rejected. We do
     * NOT let the user through on a DB outage because the suspension
     * gate in middleware can't safeguard a tenant whose
     * `suspended_reason` we couldn't read.
     */
    async signIn({ user }): Promise<boolean> {
      if (!user.email) return false;
      try {
        const resolved = await resolveTenantForEmail(user.email);
        return resolved !== null;
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error(
          "[portal signIn] resolveTenantForEmail failed; rejecting sign-in (fail-closed). " +
            "If this fires under normal traffic, check polaris_service DB connectivity.",
          err,
        );
        return false;
      }
    },

    /**
     * Mint the JWT. NextAuth calls this:
     *
     *   - Once on initial sign-in (`user` is the AdapterUser from the
     *     adapter's getUserByEmail).
     *   - On every subsequent request that needs the token (no user
     *     arg) — we return the existing token unchanged.
     *
     * Fail-closed: if resolveTenantForEmail throws on initial mint,
     * we DO NOT issue a token with stale claims. NextAuth treats a
     * thrown error here as a sign-in failure, redirecting the user
     * to the error page. The alternative (returning the token with
     * potentially empty tenant claims) would let the user past the
     * middleware's suspension check.
     */
    async jwt({ token, user }) {
      // user is defined (AdapterUser) only on initial sign-in; undefined on
      // every subsequent token refresh. Do NOT gate on user.email — NextAuth
      // v4 passes updateUser's return to the jwt callback, and our updateUser
      // had previously returned email:"" when NextAuth omitted email from the
      // call. Guard on user identity instead.
      if (!user) {
        return token;
      }
      // Belt-and-suspenders: if email is still empty (regression guard),
      // fall back to token.email which NextAuth populates from getUserByEmail.
      const emailForLookup =
        user.email || (typeof token.email === "string" ? token.email : "");
      if (!emailForLookup) {
        throw new Error(
          `[portal jwt] no email resolvable for user ${user.id} — refusing mint`,
        );
      }
      let resolved: ResolvedTenant | null;

      try {
        resolved = await resolveTenantForEmail(emailForLookup);
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error(
          "[portal jwt] resolveTenantForEmail failed; refusing to mint token (fail-closed).",
          err,
        );
        throw err;
      }
      if (resolved === null) {
        // Belt-and-braces: signIn already rejected this email; if we
        // somehow reach here without a tenant row, reject mint too.
        throw new Error(
          `[portal jwt] email ${emailForLookup} resolved no tenant — refusing mint`,
        );
      }
      token.user_id = resolved.user_id;
      token.tenant_id = resolved.tenant_id;
      token.tenant_slug = resolved.tenant_slug;
      token.role = resolved.role;
      token.plan_tier = resolved.plan_tier ?? undefined;
      token.suspended_reason = resolved.suspended_reason;
      return token;
    },

    /**
     * Expose claims to the client-side `useSession()` hook + any
     * server component that reads `getServerSession()`. With the
     * module augmentation in `types/next-auth.d.ts`, the Session
     * and JWT types carry our custom claims directly — no casts
     * needed.
     */
    async session({ session, token }) {
      session.user.id = token.user_id ?? "";
      session.tenant_id = token.tenant_id;
      session.tenant_slug = token.tenant_slug;
      session.role = token.role;
      session.plan_tier = token.plan_tier;
      session.suspended_reason = token.suspended_reason ?? null;
      return session;
    },
  },
};
