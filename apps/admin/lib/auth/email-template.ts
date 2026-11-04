/**
 * Magic-link email template for admin sign-in (Phase 9 PR-2).
 *
 * Mirrors apps/portal/lib/auth/email-template.ts; only the visible copy
 * differs ("Polaris Admin" + the 10-min expiry surfaced via config).
 * The platform_admin is the single platform admin at GA — copy is direct and
 * skips reassurance language we use in tenant-facing mail.
 *
 * Security note: NextAuth has already minted + persisted the verification
 * token (admin_magic_link_tokens via migration 055) by the time this
 * fires. The URL we send carries `token` + `email` + `callbackUrl` query
 * params; NextAuth's GET handler validates the token in
 * `useVerificationToken` (our adapter) on click. The token model is
 * already one-shot; we add no custom signing on top.
 */

import type { SendVerificationRequestParams } from "next-auth/providers/email";
import { createTransport } from "nodemailer";

import { expiryMinutes } from "@/lib/auth/config";

function assertHttpUrl(url: string): void {
  // Defence-in-depth: NextAuth builds this URL from ADMIN_NEXTAUTH_URL
  // + the verification token. Under normal config it begins with the
  // deployment's scheme. We refuse anything that isn't plain http(s)
  // so neither the HTML `href` nor the visible text can carry a
  // tainted callbackUrl that survived upstream validation.
  if (!/^https?:\/\//i.test(url)) {
    throw new Error(
      `[admin email-template] refusing to send magic link with non-http(s) URL`,
    );
  }
}

function html(params: { url: string; expiryMin: number }): string {
  const safeUrl = params.url;
  return `<!DOCTYPE html>
<html lang="en">
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0f172a;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f8fafc;">
    <tr>
      <td align="center" style="padding:32px 16px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="480" style="max-width:480px;width:100%;background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;">
          <tr>
            <td style="padding:32px 32px 8px 32px;">
              <p style="margin:0 0 4px 0;font-size:12px;letter-spacing:0.12em;text-transform:uppercase;color:#64748b;">POLARIS ADMIN</p>
              <h1 style="margin:0;font-size:20px;font-weight:600;color:#0f172a;">Sign in to the admin console</h1>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 32px;">
              <p style="margin:0 0 16px 0;font-size:14px;line-height:1.6;color:#334155;">
                Click the button below to sign in. The link works once and expires in ${params.expiryMin} minutes. It must be opened from the same network you requested it on.
              </p>
              <p style="margin:0;">
                <a href="${safeUrl}" style="display:inline-block;padding:12px 24px;background:#0f172a;color:#ffffff;text-decoration:none;border-radius:8px;font-size:14px;font-weight:500;">
                  Sign in to Polaris Admin
                </a>
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 32px 32px 32px;">
              <p style="margin:0 0 8px 0;font-size:12px;line-height:1.6;color:#64748b;">
                Or copy this URL into your browser:
              </p>
              <p style="margin:0;font-size:12px;line-height:1.6;color:#475569;word-break:break-all;">
                <a href="${safeUrl}" style="color:#475569;text-decoration:underline;">${safeUrl}</a>
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:0 32px 32px 32px;border-top:1px solid #e2e8f0;">
              <p style="margin:24px 0 0 0;font-size:12px;line-height:1.6;color:#94a3b8;">
                If you didn't request this email, ignore it. The link won't work without your active sign-in session.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>`;
}

function text(params: { url: string; expiryMin: number }): string {
  return [
    "Sign in to Polaris Admin",
    "",
    `Click the link below to sign in. It works once and expires in ${params.expiryMin} minutes. It must be opened from the same network you requested it on.`,
    "",
    params.url,
    "",
    "If you didn't request this email, ignore it.",
    "",
  ].join("\n");
}

export async function sendVerificationRequest(
  params: SendVerificationRequestParams,
): Promise<void> {
  const { identifier: email, url, provider } = params;
  const expiryMin = expiryMinutes();

  assertHttpUrl(url);

  // When ADMIN_EMAIL_SERVER_USER / _PASSWORD are empty strings (e.g. MailHog
  // in dev), NextAuth still builds auth: { user: "", pass: "" }. Nodemailer
  // then tries PLAIN auth against MailHog and fails with "Missing credentials
  // for PLAIN". Strip auth entirely when both credentials are absent so
  // nodemailer connects anonymously — which is all MailHog and unauthenticated
  // SMTP relays expect.
  type SmtpOpts = {
    host?: string;
    port?: number;
    secure?: boolean;
    auth?: { user?: string; pass?: string };
    [key: string]: unknown;
  };
  let serverConfig: SmtpOpts | typeof provider.server = provider.server;
  if (typeof provider.server === "object" && provider.server !== null) {
    const srv = provider.server as SmtpOpts;
    if (!srv.auth?.user && !srv.auth?.pass) {
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      const { auth: _auth, ...rest } = srv;
      serverConfig = rest;
    }
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const transport = createTransport(serverConfig as any);

  try {
    await transport.sendMail({
      to: email,
      from: provider.from,
      subject: "Sign in to Polaris Admin",
      text: text({ url, expiryMin }),
      html: html({ url, expiryMin }),
    });
  } catch (err) {
    const server = provider.server as { host?: string; port?: number };
    // eslint-disable-next-line no-console
    console.error(
      `[admin SMTP] sendMail failed for ${email} via ${server.host ?? "?"}:${
        server.port ?? "?"
      }`,
      err,
    );
    throw err;
  }
}
