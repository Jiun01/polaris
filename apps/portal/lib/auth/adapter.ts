/**
 * Minimal NextAuth v4 Adapter for the Polaris portal (Phase 8 PR-2).
 *
 * We implement only the methods NextAuth's EmailProvider actually calls
 * under our chosen config (JWT session strategy + magic-link only):
 *
 *   - getUserByEmail           — used by NextAuth before sending the
 *                                 verification mail; we look up
 *                                 auth.users JOIN tenant_users to
 *                                 confirm the email belongs to a
 *                                 provisioned tenant_admin.
 *   - createVerificationToken  — stores the hashed token NextAuth
 *                                 generated for the magic link.
 *   - useVerificationToken     — atomic DELETE-on-consume so the token
 *                                 can only be redeemed once.
 *
 * Everything else (createUser / updateUser / deleteUser / linkAccount /
 * unlinkAccount / createSession / getSession* / updateSession /
 * deleteSession) returns null or throws — under JWT strategy +
 * sales-only signup (ADR-012) these paths shouldn't fire. If NextAuth
 * unexpectedly invokes one, we surface it loudly rather than silently
 * succeeding.
 *
 * Anchors:
 *   - 04i §5.1 (NextAuth options shape)
 *   - ADR-012 (no public signup — signIn callback rejects unknown emails)
 *   - ADR-030 (single tenant_admin per tenant at GA)
 *   - Migration 052 (magic_link_tokens table)
 */

import type {
  Adapter,
  AdapterUser,
  VerificationToken,
} from "next-auth/adapters";

import { portalQuery } from "@/lib/auth/db";

function notImplemented(method: string): never {
  // Loud failure rather than silent — if NextAuth's flow grows a code
  // path that calls these, we want a noisy 500 + log line during
  // development, not a silent regression in prod.
  throw new Error(
    `[portal NextAuth adapter] ${method}() is not implemented. ` +
      "Polaris GA uses JWT session strategy + EmailProvider only " +
      "(ADR-030); other adapter methods should not be reached. " +
      "If you see this, an upstream NextAuth change has expanded the " +
      "call surface — review the adapter contract.",
  );
}

export function PortalAdapter(): Adapter {
  return {
    /**
     * Look up a user by their email. NextAuth calls this BEFORE sending
     * the verification email; we exploit that to gate: an email that
     * doesn't resolve to a tenant_users row is rejected at the signIn
     * callback step (returning null here means "no such user" — which
     * combined with `allowDangerousEmailAccountLinking: false`
     * surfaces as a normal "user not found" path for the signIn
     * callback to reject).
     *
     * The JOIN against tenant_users is the load-bearing security
     * check — auth.users alone (Supabase-managed) might hold users who
     * were never provisioned through Polaris's admin path. We require
     * a tenant_users row to confirm an active membership exists.
     *
     * The shape returned matches NextAuth's AdapterUser. We populate
     * email + id (auth.users.id). NextAuth will write image / name /
     * emailVerified into the JWT in the jwt callback — those come from
     * the tenants / tenant_users rows joined in options.ts callbacks,
     * not from this adapter call.
     */
    async getUserByEmail(email: string): Promise<AdapterUser | null> {
      const normalized = email.trim().toLowerCase();
      // Use the SECURITY DEFINER helper (migration 066) so polaris_service
      // doesn't need direct USAGE on the auth schema — Supabase doesn't
      // allow that grant for custom roles.
      const { rows } = await portalQuery<{
        id: string;
        email: string;
      }>(`SELECT id, email FROM public.get_portal_user_by_email($1)`, [
        normalized,
      ]);
      if (rows.length === 0) return null;
      const r = rows[0];
      return {
        id: r.id,
        email: r.email,
        // emailVerified is part of NextAuth's AdapterUser contract.
        // Supabase auth.users carries email_confirmed_at; we treat any
        // tenant_users membership as implicitly email-verified because
        // admin provisioning won't create a tenant_users row for an
        // unverified email. Returning a fixed past date avoids re-
        // verification cycles inside NextAuth.
        emailVerified: new Date(0),
      };
    },

    async createVerificationToken(
      token: VerificationToken,
    ): Promise<VerificationToken> {
      const normalized = token.identifier.trim().toLowerCase();
      try {
        await portalQuery(
          `
          INSERT INTO magic_link_tokens (identifier, token, expires)
          VALUES ($1, $2, $3)
          `,
          [normalized, token.token, token.expires],
        );
      } catch (err) {
        // Surface labelled context. PK conflicts on (identifier, token)
        // are cryptographically near-impossible (token is a SHA-256
        // hash) but could fire under e.g. a clock-rewind or a stuck
        // pool returning a duplicate response. Pool timeout / network
        // errors look the same here as DB-side rejections. Logging
        // host/code is the only signal an operator has if magic links
        // stop being created.
        // portalQuery wraps transient connection failures as DbUnavailableError
        // (code on `.cause`); a real Postgres error (e.g. PK conflict) arrives
        // unwrapped with its own `.code`. Read whichever is present.
        const pgErr = err as { code?: string; cause?: { code?: string } };
        const code = pgErr.code ?? pgErr.cause?.code ?? "?";
        // eslint-disable-next-line no-console
        console.error(
          `[portal adapter] createVerificationToken failed for ${normalized} ` +
            `(pg code=${code})`,
          err,
        );
        throw err;
      }
      return { ...token, identifier: normalized };
    },

    /**
     * Atomic DELETE-on-consume. Returns the row that was deleted, or
     * null if the token didn't exist (already consumed, never created,
     * or expired-and-GC'd). NextAuth treats null as "invalid token"
     * and shows the user-friendly error page — which is what we want.
     *
     * Expiry is checked downstream by NextAuth — it compares `expires`
     * to now() and rejects if past. We could pre-filter here for a
     * marginal latency improvement; we don't, so the row counts in
     * the test suite stay deterministic.
     */
    async useVerificationToken(params: {
      identifier: string;
      token: string;
    }): Promise<VerificationToken | null> {
      const normalized = params.identifier.trim().toLowerCase();
      const { rows } = await portalQuery<{
        identifier: string;
        token: string;
        expires: Date;
      }>(
        `
        DELETE FROM magic_link_tokens
         WHERE identifier = $1 AND token = $2
        RETURNING identifier, token, expires
        `,
        [normalized, params.token],
      );
      if (rows.length === 0) return null;
      const r = rows[0];
      return {
        identifier: r.identifier,
        token: r.token,
        expires: r.expires,
      };
    },

    // --- Unused under our config — fail loudly if reached ----------

    async createUser(): Promise<AdapterUser> {
      // We never auto-create users — provisioning is admin-only per
      // ADR-012. If signIn lets an unknown email through to here,
      // that's a regression.
      return notImplemented("createUser");
    },

    async getUser(): Promise<AdapterUser | null> {
      // NextAuth calls this when looking up a user by adapter id. With
      // EmailProvider + JWT strategy this path doesn't fire because
      // the user lookup happens via getUserByEmail.
      return notImplemented("getUser");
    },

    async getUserByAccount(): Promise<AdapterUser | null> {
      // OAuth-only path. We ship magic-link only at GA.
      return notImplemented("getUserByAccount");
    },

    async updateUser(
      user: Partial<AdapterUser> & { id: string },
    ): Promise<AdapterUser> {
      // NextAuth calls updateUser during the email-callback to persist
      // emailVerified. It passes only { id, emailVerified } — no email field.
      // We must return the full AdapterUser (including email) because NextAuth
      // uses this return value to build the JWT base token. Returning email:""
      // poisons the JWT and the session shows user.email="" which breaks the
      // layout's getServerSession check. Look up the email by ID via the same
      // SECURITY DEFINER path used in migration 067.
      let email = user.email ?? "";
      if (!email) {
        const { rows } = await portalQuery<{ email: string | null }>(
          `SELECT public.get_portal_user_email_by_id($1) AS email`,
          [user.id],
        );
        email = rows[0]?.email ?? "";
      }
      return {
        id: user.id,
        email,
        emailVerified: user.emailVerified ?? new Date(0),
      };
    },

    async linkAccount(): Promise<void> {
      return notImplemented("linkAccount");
    },

    // Session methods — JWT strategy means NextAuth manages session in
    // the browser cookie, never the database. These should never be
    // called; if NextAuth ever switches strategy at runtime they will.
    async createSession() {
      return notImplemented("createSession");
    },
    async getSessionAndUser() {
      return notImplemented("getSessionAndUser");
    },
    async updateSession() {
      return notImplemented("updateSession");
    },
    async deleteSession() {
      return notImplemented("deleteSession");
    },
  };
}
