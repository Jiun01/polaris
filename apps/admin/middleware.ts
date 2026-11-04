/**
 * Admin middleware (Phase 9 PR-2).
 *
 * Mirrors apps/portal/middleware.ts but with admin-specific posture:
 *
 *   - One job at PR-2: auth guard. Unauthenticated requests redirect
 *     to /login (preserving callbackUrl).
 *
 *   - There is no "suspension" concept for the admin app — admin
 *     either has a valid JWT or doesn't. (Tenant-level suspension is
 *     the portal's concern.)
 *
 *   - No "authenticated user hitting auth screen bounces to /tenants"
 *     redirect yet — Sidebar shell + /tenants screen land in PR-3.
 *     Bouncing to a non-existent route here would create a redirect
 *     loop at PR-2 close. PR-3 adds that bounce.
 *
 * The matcher excludes:
 *   - /api/auth/*     — NextAuth's own endpoints, MUST run un-gated.
 *   - /_next/*        — Next.js static + image optimizer.
 *   - /brand/*        — public brand assets.
 *   - /favicon.ico    — root favicon shortcut.
 *
 * Public paths inside the matcher (auth pages):
 *   - /login
 *   - /verify-request
 *
 * SECURITY: NextAuth's `getToken` returns the JWT only if it was
 * signed with NEXTAUTH_SECRET and not expired. We don't re-verify
 * platform_admins.active on every request — that'd force a DB query
 * per navigation. The jwt callback re-resolves the row on token mint;
 * a soft-deactivated admin keeps access until the JWT expires
 * (max 1 day per ADMIN_SESSION_MAX_AGE_S). Acceptable at GA's single-
 * admin model.
 */

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { getToken } from "next-auth/jwt";

import { isSecureDeployment } from "@/lib/auth/config";

const AUTH_PUBLIC_PATHS = new Set<string>(["/login", "/verify-request"]);

// One-shot warning if ADMIN_NEXTAUTH_SECRET (or its alias NEXTAUTH_SECRET)
// is missing: every getToken call would return null and every request
// would redirect to /login. Without this log the operator sees a redirect
// loop with no signal as to why.
if (!process.env.NEXTAUTH_SECRET && !process.env.ADMIN_NEXTAUTH_SECRET) {
  // eslint-disable-next-line no-console
  console.error(
    "[admin middleware] neither NEXTAUTH_SECRET nor ADMIN_NEXTAUTH_SECRET " +
      "is set — every session will appear invalid and every request will " +
      "redirect to /login. Set ADMIN_NEXTAUTH_SECRET before deploying.",
  );
}

export async function middleware(req: NextRequest): Promise<NextResponse> {
  const { pathname, search } = req.nextUrl;

  // Middleware runs in the Edge Runtime — process.env mutations made by
  // syncAuthEnvAliases() in the Node.js route handler are NOT visible here.
  // Always prefer ADMIN_NEXTAUTH_SECRET (the authoritative admin secret)
  // so the JWT verification uses the same secret that NextAuth signed with.
  // NEXTAUTH_SECRET in the container env is the portal secret (loaded from
  // the shared root .env) and must not be used to verify admin JWTs.
  const secret =
    process.env.ADMIN_NEXTAUTH_SECRET ?? process.env.NEXTAUTH_SECRET ?? "";

  const token = await getToken({
    req,
    secret,
    secureCookie: isSecureDeployment(),
  });

  if (!token) {
    if (AUTH_PUBLIC_PATHS.has(pathname)) {
      return NextResponse.next();
    }
    const loginUrl = new URL("/login", req.url);
    if (pathname !== "/") {
      loginUrl.searchParams.set("callbackUrl", pathname + search);
    }
    return NextResponse.redirect(loginUrl);
  }

  // Authenticated user already past the auth wall — let the request
  // through. Bouncing /login → /tenants for an authenticated session
  // is a PR-3 concern (when /tenants exists).
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!api/auth|_next/static|_next/image|brand|favicon.ico).*)"],
};
