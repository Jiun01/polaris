/**
 * Portal middleware (Phase 8 PR-2).
 *
 * Runs before every request to the matched paths. Three ordered jobs:
 *
 *   1. Auth guard.    Unauthenticated → /login (preserving callbackUrl).
 *   2. Suspension takeover. tenants.suspended_reason='payment_overdue'
 *      → /pay (every route except /pay itself).
 *   3. Onboarding stub. (Full logic lands in PR-5 when the channels
 *      and first-flow tables can be joined cheaply at edge time.)
 *
 * Order matters — see 04i §9 gotchas. The auth check must run BEFORE
 * the suspension check; otherwise an unauthenticated request gets
 * redirected to /pay first, then bounces to /login, which looks like
 * a broken redirect chain to the user.
 *
 * The matcher excludes:
 *   - /api/auth/*     — NextAuth's own endpoints, including the
 *                       magic-link callback. These MUST run un-gated.
 *   - /_next/*        — Next.js static + image optimizer
 *   - /brand/*        — public brand assets (logo, favicon, OG)
 *   - /favicon.ico    — root favicon shortcut
 *   - /login,         — auth screens are public
 *   - /verify-request
 *
 * SECURITY: NextAuth's `getToken` returns the JWT only if it was
 * signed with NEXTAUTH_SECRET and not expired. We don't re-verify
 * tenant_users membership here on every request — that'd force a DB
 * query per navigation. The jwt callback in options.ts re-resolves
 * the tenant on token mint; an admin who is later removed from
 * tenant_users keeps access until the token expires (max 30 days
 * per PORTAL_SESSION_MAX_AGE_S). Acceptable at GA's single-admin model.
 */

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { getToken } from "next-auth/jwt";

import { isSecureDeployment } from "@/lib/auth/config";

const AUTH_PUBLIC_PATHS = new Set<string>(["/login", "/verify-request"]);

// One-shot warning if NEXTAUTH_SECRET is missing: every getToken call
// will return null and every request will redirect to /login. Without
// this log, the operator sees a redirect loop with no signal as to why.
if (!process.env.NEXTAUTH_SECRET) {
  // eslint-disable-next-line no-console
  console.error(
    "[portal middleware] NEXTAUTH_SECRET is not set — every session " +
      "will appear invalid and every request will redirect to /login. " +
      "Set this env var before deploying.",
  );
}

export async function middleware(req: NextRequest): Promise<NextResponse> {
  const { pathname, search } = req.nextUrl;

  // Phase 9 PR-5: check the impersonation cookie FIRST (before the auth
  // redirect) so it can be forwarded as a header on public paths too.
  // This lets ImpersonationBanner render even when the admin is viewing
  // the portal without an active portal session (e.g. E2E test).
  const impersonationCookie = req.cookies.get("pol_impersonation");
  const isImpersonating = !!impersonationCookie?.value;

  // 1. Auth guard. `secureCookie` derives from NEXTAUTH_URL's scheme
  // so we read the right cookie name in both prod (HTTPS →
  // `__Secure-next-auth.session-token`) and dev (HTTP →
  // `next-auth.session-token`). Without this, the prod deploy would
  // look for the dev cookie name and never find a valid token.
  const token = await getToken({
    req,
    secret: process.env.NEXTAUTH_SECRET,
    secureCookie: isSecureDeployment(),
  });

  if (!token) {
    if (AUTH_PUBLIC_PATHS.has(pathname)) {
      if (isImpersonating) {
        // Forward the impersonation signal as a REQUEST header so Server
        // Components can read it via headers(). Setting only a response
        // header is insufficient — SSR reads incoming request headers.
        const fwdHeaders = new Headers(req.headers);
        fwdHeaders.set("x-polaris-impersonating", "1");
        return NextResponse.next({ request: { headers: fwdHeaders } });
      }
      return NextResponse.next();
    }
    const loginUrl = new URL("/login", req.url);
    // Preserve the original target so post-signin lands there. We skip
    // this for the root and the suspended page (callbackUrl='/' is a
    // no-op; /pay is meaningless for an unauthenticated user).
    if (pathname !== "/" && pathname !== "/pay") {
      loginUrl.searchParams.set("callbackUrl", pathname + search);
    }
    return NextResponse.redirect(loginUrl);
  }

  // Authenticated user hitting an auth screen → bounce to dashboard.
  // This is a tiny UX win: signed-in tenant clicking /login doesn't
  // see the form again. Also keeps the URL space tidy.
  if (AUTH_PUBLIC_PATHS.has(pathname)) {
    return NextResponse.redirect(new URL("/dashboard", req.url));
  }

  // 2. Suspension takeover.
  if (token.suspended_reason === "payment_overdue" && pathname !== "/pay") {
    return NextResponse.redirect(new URL("/pay", req.url));
  }

  // An un-suspended user who somehow lands on /pay (state changed
  // mid-session, or bookmark) bounces back to dashboard.
  if (token.suspended_reason !== "payment_overdue" && pathname === "/pay") {
    return NextResponse.redirect(new URL("/dashboard", req.url));
  }

  // 3. Onboarding redirect — stub. PR-5 will check the tenant's first
  // channel + first published flow and redirect from /dashboard to
  // /onboarding when both are missing. We don't run that query at edge
  // time in PR-2 because the tenant_channels + flow_definitions joins
  // aren't worth the per-request cost when the dashboard server
  // component can do the same check on its own load.

  // Phase 9 PR-5: surface the pol_impersonation cookie as a request
  // header so the portal ImpersonationBanner server component can read
  // it. The cookie is HttpOnly — JS cannot access it — but Next.js
  // middleware can inspect cookies and forward a non-sensitive signal.
  // We forward only the presence ("1"), not the raw JWT, so the tenant
  // user's view cannot reconstruct admin identity from the header.
  if (isImpersonating) {
    const fwdHeaders = new Headers(req.headers);
    fwdHeaders.set("x-polaris-impersonating", "1");
    return NextResponse.next({ request: { headers: fwdHeaders } });
  }

  return NextResponse.next();
}

// Match every path except API auth endpoints, Next internals, and
// public assets. The bracketed alternation is the standard Next 14
// idiom for "exclude these prefixes."
export const config = {
  matcher: ["/((?!api/auth|_next/static|_next/image|brand|favicon.ico).*)"],
};
