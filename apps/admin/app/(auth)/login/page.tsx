/**
 * Admin login screen (Phase 9 PR-2).
 *
 * Mirrors apps/portal/app/(auth)/login/page.tsx. Differences:
 *
 *   - Brand line + heading say "Polaris Admin" so the operator can
 *     visually distinguish from the tenant portal.
 *   - callbackUrl defaults to /tenants (admin's first natural landing
 *     surface), not /dashboard.
 *   - Error copy mentions IP-bind in the AccessDenied message —
 *     "this email isn't a platform admin" + "or you opened the link
 *     from a different network than you requested it from".
 *   - No "contact hello@" footer; admin is sales-led INTO platform_admins
 *     by direct out-of-band operator action, not a CTA.
 *
 * The form posts to NextAuth's `/api/auth/signin/email` exactly like
 * the portal. CSRF token fetched server-side via /api/auth/csrf so we
 * keep this a pure Server Component.
 */

import { cookies, headers } from "next/headers";

async function getCsrfToken(): Promise<string> {
  // Prefer NEXTAUTH_URL_INTERNAL for server-side CSRF fetch so requests
  // from inside the Docker container resolve via Docker's internal DNS
  // (e.g. http://polaris-admin:3001) instead of the public hostname
  // (http://admin.localhost) which doesn't resolve inside the container.
  // Falls back to ADMIN_NEXTAUTH_URL / NEXTAUTH_URL for non-Docker envs.
  // Headers are the final fallback for bare dev (security review Finding 6).
  const configured =
    process.env.NEXTAUTH_URL_INTERNAL ??
    process.env.ADMIN_NEXTAUTH_URL ??
    process.env.NEXTAUTH_URL ??
    "";
  let origin = configured;
  if (!origin) {
    const h = headers();
    const host = h.get("x-forwarded-host") ?? h.get("host");
    const proto = h.get("x-forwarded-proto") ?? "http";
    origin = host ? `${proto}://${host}` : "";
  }
  if (!origin) {
    // eslint-disable-next-line no-console
    console.error(
      "[admin /login] cannot derive origin for CSRF fetch — " +
        "neither x-forwarded-host nor host header present, and " +
        "ADMIN_NEXTAUTH_URL is unset. Form submit will be CSRF-rejected.",
    );
    return "";
  }
  try {
    // Forward the browser's cookies so NextAuth returns the token that
    // matches the already-set next-auth.csrf-token cookie in the browser
    // rather than generating a new (mismatched) pair. Without forwarding,
    // the server-side CSRF fetch creates an isolated token that the browser
    // never receives, causing CSRF validation to fail on form submit.
    const cookieHeader = cookies().toString();
    const res = await fetch(`${origin}/api/auth/csrf`, {
      cache: "no-store",
      ...(cookieHeader ? { headers: { cookie: cookieHeader } } : {}),
    });
    if (!res.ok) {
      // eslint-disable-next-line no-console
      console.error(
        `[admin /login] /api/auth/csrf returned ${res.status}; form will submit with empty token.`,
      );
      return "";
    }
    const data = (await res.json()) as { csrfToken?: string };
    return data.csrfToken ?? "";
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error(
      "[admin /login] CSRF fetch threw; form will submit with empty token.",
      err,
    );
    return "";
  }
}

export default async function LoginPage({
  searchParams,
}: {
  searchParams?: { error?: string; callbackUrl?: string };
}) {
  const csrfToken = await getCsrfToken();
  // Sanitize callbackUrl (security review Finding 5 — open-redirect):
  // Only accept relative paths starting with a single slash followed by
  // a non-slash character. This rejects protocol-relative '//evil.com',
  // absolute 'https://evil.com', empty string, and the literal '/'.
  // Anything else falls back to the default safe landing.
  const rawCallback = searchParams?.callbackUrl ?? "";
  const callbackUrl = /^\/[^/]/.test(rawCallback) ? rawCallback : "/tenants";
  const error = searchParams?.error;

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-8 shadow-sm dark:border-slate-800 dark:bg-slate-900">
      <h1 className="mb-2 text-xl font-semibold text-slate-900 dark:text-slate-50">
        Sign in to the admin console
      </h1>
      <p className="mb-6 text-sm text-slate-600 dark:text-slate-400">
        Enter your platform-admin email. We&apos;ll send you a one-time sign-in
        link.
      </p>

      {error ? (
        <div
          role="alert"
          className="mb-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900 dark:border-rose-900/40 dark:bg-rose-950/40 dark:text-rose-200"
        >
          {error === "AccessDenied"
            ? "That email isn't an active platform admin, or you opened the link from a different network than you requested it from. Request a fresh link from the same network."
            : "We couldn't send the sign-in link. Try again in a few minutes."}
        </div>
      ) : null}

      <form method="post" action="/api/auth/signin/email" className="space-y-4">
        <input type="hidden" name="csrfToken" value={csrfToken} />
        <input type="hidden" name="callbackUrl" value={callbackUrl} />

        <div>
          <label
            htmlFor="email"
            className="mb-1.5 block text-sm font-medium text-slate-800 dark:text-slate-200"
          >
            Admin email
          </label>
          <input
            id="email"
            name="email"
            type="email"
            required
            autoComplete="email"
            placeholder="you@getpolaris.my"
            className="block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm focus:border-slate-400 focus:outline-none focus:ring-2 focus:ring-slate-200 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100 dark:focus:ring-slate-700"
          />
        </div>

        <button
          type="submit"
          className="inline-flex w-full items-center justify-center rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white transition hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-slate-300 focus:ring-offset-2 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
        >
          Send sign-in link
        </button>
      </form>

      <p className="mt-6 text-xs text-slate-500 dark:text-slate-400">
        The link expires in 10 minutes and must be opened from the same network
        you requested it on.
      </p>
    </div>
  );
}
