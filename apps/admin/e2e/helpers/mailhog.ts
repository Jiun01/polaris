/**
 * MailHog API client for E2E specs (Phase 9 PR-12).
 *
 * Thin wrapper around the MailHog v2 search endpoint. Used by auth specs
 * to intercept magic-link emails without a real SMTP relay.
 *
 * ADMIN_MAILHOG_API_URL must be set (e.g. http://localhost:8025).
 * Specs guard with test.skip(() => !MAILHOG_API, ...) before calling.
 */

import type { APIRequestContext } from "@playwright/test";

export const MAILHOG_API = process.env.ADMIN_MAILHOG_API_URL;

/**
 * Poll MailHog until a magic-link email arrives for `email`, then extract
 * and return the verification URL.
 */
export async function pollForAdminMagicLink(
  request: APIRequestContext,
  email: string,
  timeoutMs = 30_000,
): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 500));
    const res = await request.get(
      `${MAILHOG_API}/api/v2/search?kind=to&query=${encodeURIComponent(email)}`,
    );
    const body = (await res.json()) as {
      items: { Content: { Body: string } }[];
    };
    if (body.items.length > 0) {
      // The email body uses Content-Transfer-Encoding: quoted-printable.
      // QP wraps long lines with `=\r\n` soft line breaks and encodes `=`
      // as `=3D`. Decode both before running the URL regex so the full
      // magic-link URL (token + email params) is captured in one pass.
      // (Same logic as global-setup.ts pollForMagicLink.)
      const rawBody = body.items[0].Content.Body;
      const decoded = rawBody
        .replace(/=\r\n/g, "")
        .replace(/=\n/g, "")
        .replace(/=([0-9A-Fa-f]{2})/g, (_: string, hex: string) =>
          String.fromCharCode(parseInt(hex, 16)),
        );
      // NextAuth v4 magic-link URL: /api/auth/callback/email?token=...
      // Terminators: whitespace, quotes, and angle-brackets cover plain-text
      // lines and HTML href values in the decoded body.
      const match = decoded.match(
        /https?:\/\/[^\s"<>]+api\/auth\/callback\/email[^\s"<>]*/,
      );
      if (match) return match[0];
    }
    await new Promise((r) => setTimeout(r, 1_000));
  }
  throw new Error(
    `No admin magic-link email arrived for ${email} within ${timeoutMs}ms`,
  );
}

/** Delete all MailHog messages (call in beforeEach to isolate tests). */
export async function clearMailhog(request: APIRequestContext): Promise<void> {
  await request.delete(`${MAILHOG_API}/api/v1/messages`);
}
