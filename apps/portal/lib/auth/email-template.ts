/**
 * Magic-link email template (Phase 8 PR-2).
 *
 * Polaris-branded HTML + plain-text fallback. Single sendVerificationRequest
 * function wired into NextAuth's EmailProvider config. Both bodies carry the
 * sign-in URL; the HTML version is the primary surface and the text
 * version exists so spam filters don't penalise the message and so
 * accessible-mail clients can read it.
 *
 * Copy follows the brand voice from `_artifacts/portal_design_prompt.md`:
 * calm, trustworthy, plain English. The 45-year-old SME owner is the
 * mental model — no marketing flourish, no engineer jargon.
 *
 * Security note: NextAuth has already minted + persisted the verification
 * token by the time this fires. The URL we send carries `token` + `email`
 * + `callbackUrl` query params; NextAuth's GET handler validates the
 * token in `useVerificationToken` (our adapter) on click. We don't add
 * any custom signing here — NextAuth's token model is already one-shot.
 */

import type { SendVerificationRequestParams } from "next-auth/providers/email";
import { createTransport } from "nodemailer";

import { expiryMinutes } from "@/lib/auth/config";

function assertHttpUrl(url: string): void {
  // Defence-in-depth. NextAuth builds this URL from NEXTAUTH_URL + the
  // verification token; under normal config it will start with the
  // deployment's scheme. We refuse to interpolate anything that doesn't
  // look like a plain http(s) URL into both the HTML `href` and the
  // visible text — protects against the unlikely case that NextAuth's
  // URL builder is fed a tainted callbackUrl that survives upstream
  // validation.
  if (!/^https?:\/\//i.test(url)) {
    throw new Error(
      `[portal email-template] refusing to send magic link with non-http(s) URL`,
    );
  }
}

function html(params: { url: string; expiryMin: number }): string {
  // Inline styles only — email clients strip <style> blocks unreliably.
  // Single column, max-width 480, no images (deliverability + dark-mode
  // friendliness). The URL is validated by assertHttpUrl() above; this
  // template renders it verbatim with no user-controlled text.
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
              <p style="margin:0 0 4px 0;font-size:12px;letter-spacing:0.12em;text-transform:uppercase;color:#64748b;">POLARIS</p>
              <h1 style="margin:0;font-size:20px;font-weight:600;color:#0f172a;">Sign in to your Polaris portal</h1>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 32px;">
              <p style="margin:0 0 16px 0;font-size:14px;line-height:1.6;color:#334155;">
                Click the button below to sign in. The link will work once and expires in ${params.expiryMin} minutes.
              </p>
              <p style="margin:0;">
                <a href="${safeUrl}" style="display:inline-block;padding:12px 24px;background:#0f172a;color:#ffffff;text-decoration:none;border-radius:8px;font-size:14px;font-weight:500;">
                  Sign in to Polaris
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
                If you didn't request this email, you can safely ignore it &mdash; no one can sign in without clicking the link.
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
    "Sign in to Polaris",
    "",
    `Click the link below to sign in. It works once and expires in ${params.expiryMin} minutes.`,
    "",
    params.url,
    "",
    "If you didn't request this email, you can safely ignore it.",
    "",
  ].join("\n");
}

/**
 * NextAuth EmailProvider hands us the verification request shape; we
 * compose the message and hand it to nodemailer. Throwing here cascades
 * back to the user as "EmailSignin" error on the verify-request page.
 */
export async function sendVerificationRequest(
  params: SendVerificationRequestParams,
): Promise<void> {
  const { identifier: email, url, provider } = params;
  const expiryMin = expiryMinutes();

  assertHttpUrl(url);

  // Strip auth entirely when both credentials are absent so nodemailer
  // connects anonymously — which is all MailHog and unauthenticated
  // SMTP relays expect. Passing auth: { user: "", pass: "" } triggers
  // nodemailer's PLAIN handshake and MailHog returns EAUTH.
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
      subject: "Sign in to your Polaris portal",
      text: text({ url, expiryMin }),
      html: html({ url, expiryMin }),
    });
  } catch (err) {
    // The user lands on /verify-request expecting an email that
    // never arrives if we don't log here. NextAuth catches the
    // rethrown error and surfaces the EmailSignin error page; we
    // want the OPERATOR signal so the magic-link outage is visible
    // in logs.
    const server = provider.server as { host?: string; port?: number };
    // eslint-disable-next-line no-console
    console.error(
      `[portal SMTP] sendMail failed for ${email} via ${server.host ?? "?"}:${
        server.port ?? "?"
      }`,
      err,
    );
    throw err;
  }
}
