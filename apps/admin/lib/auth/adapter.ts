/**
 * Minimal NextAuth v4 Adapter for the Polaris admin (Phase 9 PR-2).
 *
 * Mirrors `apps/portal/lib/auth/adapter.ts` exactly, except:
 *
 *   - getUserByEmail looks up `platform_admins` (migration 054), not
 *     auth.users JOIN tenant_users. The platform_admin row must have
 *     active = TRUE — soft-deactivated admins can't request links.
 *
 *   - Verification tokens go to `admin_magic_link_tokens` (migration 055),
 *     NOT the portal's `magic_link_tokens`. Disjoint storage means a bug
 *     in either app's adapter cannot consume the other's tokens.
 *
 * Methods implemented (only what NextAuth EmailProvider + JWT strategy
 * actually invoke):
 *
 *   - getUserByEmail           — gates on platform_admins.active
 *   - createVerificationToken  — INSERT into admin_magic_link_tokens
 *   - useVerificationToken     — atomic DELETE-RETURNING (single-use)
 *
 * Everything else throws loudly via notImplemented() — under JWT strategy
 * + magic-link-only + no public signup, those code paths shouldn't fire.
 *
 * Anchors:
 *   - 04j §5.1 (admin NextAuth options shape)
 *   - ADR-012 (no public signup — signIn callback rejects unknown emails)
 *   - ADR-030 Amendment 2026-05-20 (magic-link only at GA; no MFA)
 *   - Migration 054 (platform_admins) + Migration 055 (admin_magic_link_tokens)
 */

import type {
  Adapter,
  AdapterUser,
  VerificationToken,
} from "next-auth/adapters";

import { getAdminPool } from "@/lib/auth/db";

function notImplemented(method: string): never {
  // Loud failure rather than silent — if NextAuth's flow grows a code
  // path that calls these, we want a noisy 500 + log line during
  // development, not a silent regression in prod.
  // eslint-disable-next-line no-console
  console.error(
    `[admin adapter] UNEXPECTED CALL to ${method}(). ` +
      "This method is not implemented (Polaris admin uses JWT + EmailProvider only, " +
      "ADR-030 Am.2026-05-20). If createUser() was called, check that " +
      "platform_admins has an active row for the sign-in email " +
      "(getUserByEmail returned null → NextAuth fell through to createUser). " +
      "Run: uv run python apps/api/scripts/seed_e2e_admin.py",
  );
  throw new Error(
    `[admin NextAuth adapter] ${method}() is not implemented. ` +
      "Polaris admin GA uses JWT session strategy + EmailProvider only " +
      "(ADR-030 Am.2026-05-20); other adapter methods should not be " +
      "reached. If you see this, an upstream NextAuth change has " +
      "expanded the call surface — review the adapter contract.",
  );
}

export function AdminAdapter(): Adapter {
  return {
    /**
     * Look up a platform_admin by email. NextAuth calls this BEFORE
     * sending the verification email; we use it as the first auth gate:
     * an email that doesn't resolve to an ACTIVE platform_admins row is
     * rejected (return null -> NextAuth fires signIn callback with
     * null user -> signIn returns false -> AccessDenied).
     *
     * The `active = TRUE` filter is load-bearing: a soft-deactivated
     * admin (departure cleanup) must NOT be able to request fresh links.
     * Migration 054 has a partial index on (email) WHERE active = TRUE
     * to keep this lookup index-only.
     *
     * The returned AdapterUser carries id (platform_admins.id) and
     * email. NextAuth doesn't use any other fields here under our config.
     */
    async getUserByEmail(email: string): Promise<AdapterUser | null> {
      const normalized = email.trim().toLowerCase();
      // eslint-disable-next-line no-console
      console.log(`[admin adapter] getUserByEmail(${normalized})`);
      let rows: { id: string; email: string }[];
      try {
        const result = await getAdminPool().query<{
          id: string;
          email: string;
        }>(
          `
          SELECT id, email
            FROM platform_admins
           WHERE lower(email) = $1
             AND active = TRUE
           LIMIT 1
          `,
          [normalized],
        );
        rows = result.rows;
      } catch (err) {
        const pgErr = err as { code?: string };
        // eslint-disable-next-line no-console
        console.error(
          `[admin adapter] getUserByEmail failed for ${normalized} (pg code=${pgErr.code ?? "?"})`,
          err,
        );
        throw err;
      }
      // eslint-disable-next-line no-console
      console.log(
        `[admin adapter] getUserByEmail → ${rows.length} row(s) for ${normalized}`,
      );
      if (rows.length === 0) return null;
      const r = rows[0];
      return {
        id: r.id,
        email: r.email,
        // NextAuth's AdapterUser contract requires emailVerified. We treat
        // any active platform_admins row as implicitly verified (admin
        // provisioning is manual and out-of-band — only the operator
        // creates platform_admins rows). Returning a fixed past date
        // avoids re-verification cycles inside NextAuth.
        emailVerified: new Date(0),
      };
    },

    async createVerificationToken(
      token: VerificationToken,
    ): Promise<VerificationToken> {
      const normalized = token.identifier.trim().toLowerCase();
      try {
        await getAdminPool().query(
          `
          INSERT INTO admin_magic_link_tokens (identifier, token, expires)
          VALUES ($1, $2, $3)
          `,
          [normalized, token.token, token.expires],
        );
      } catch (err) {
        // Surface labelled context. PK conflicts on (identifier, token)
        // are cryptographically near-impossible (token is sha256-hashed)
        // but could fire under clock-rewind or pool returning a duplicate.
        // Pool timeout / network errors look the same here as DB-side
        // rejections. Logging host/code is the only signal an operator
        // has if admin magic links stop being created.
        const pgErr = err as { code?: string };
        // eslint-disable-next-line no-console
        console.error(
          `[admin adapter] createVerificationToken failed for ${normalized} ` +
            `(pg code=${pgErr.code ?? "?"})`,
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
     * Expiry is checked downstream by NextAuth (`expires` vs now()).
     * We could pre-filter here for a marginal latency improvement; we
     * don't, so test-suite row counts stay deterministic.
     */
    async useVerificationToken(params: {
      identifier: string;
      token: string;
    }): Promise<VerificationToken | null> {
      const normalized = params.identifier.trim().toLowerCase();
      // eslint-disable-next-line no-console
      console.log(`[admin adapter] useVerificationToken(${normalized})`);
      let rows: { identifier: string; token: string; expires: Date }[];
      try {
        const result = await getAdminPool().query<{
          identifier: string;
          token: string;
          expires: Date;
        }>(
          `
          DELETE FROM admin_magic_link_tokens
           WHERE identifier = $1 AND token = $2
          RETURNING identifier, token, expires
          `,
          [normalized, params.token],
        );
        rows = result.rows;
      } catch (err) {
        const pgErr = err as { code?: string };
        // eslint-disable-next-line no-console
        console.error(
          `[admin adapter] useVerificationToken failed for ${normalized} ` +
            `(pg code=${pgErr.code ?? "?"})`,
          err,
        );
        throw err;
      }
      // eslint-disable-next-line no-console
      console.log(
        `[admin adapter] useVerificationToken → ${rows.length} row(s) for ${normalized}`,
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
      // platform_admins rows are created manually by the operator
      // (out-of-band SQL or admin-side operator-only provisioning UI
      // TBD post-GA). NextAuth must never auto-create.
      return notImplemented("createUser");
    },

    async getUser(): Promise<AdapterUser | null> {
      return notImplemented("getUser");
    },

    async getUserByAccount(): Promise<AdapterUser | null> {
      // OAuth-only path. Magic-link only at GA.
      return notImplemented("getUserByAccount");
    },

    /**
     * NextAuth v4.24+ calls updateUser() after a successful email
     * callback to stamp emailVerified on the user record.  We have no
     * emailVerified column in platform_admins (active = TRUE is the
     * gate); treat this as a no-op by re-fetching the canonical row
     * and returning it with emailVerified fixed to new Date(0).
     */
    async updateUser(
      user: Partial<AdapterUser> & Pick<AdapterUser, "id">,
    ): Promise<AdapterUser> {
      // eslint-disable-next-line no-console
      console.log(
        `[admin adapter] updateUser(${user.id}) — no-op (platform_admins has no emailVerified column)`,
      );
      const result = await getAdminPool().query<{ id: string; email: string }>(
        `
        SELECT id, email
          FROM platform_admins
         WHERE id = $1
           AND active = TRUE
         LIMIT 1
        `,
        [user.id],
      );
      if (result.rows.length === 0) {
        // Shouldn't happen — caller just verified the user exists
        // eslint-disable-next-line no-console
        console.error(
          `[admin adapter] updateUser: no active platform_admin row for id=${user.id}`,
        );
        throw new Error(
          `[admin adapter] updateUser: no active platform_admin row for id=${user.id}`,
        );
      }
      const r = result.rows[0];
      return {
        id: r.id,
        email: r.email,
        emailVerified: new Date(0),
      };
    },

    async linkAccount(): Promise<void> {
      return notImplemented("linkAccount");
    },

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
