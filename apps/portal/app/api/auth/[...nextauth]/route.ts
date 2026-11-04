/**
 * NextAuth catch-all route handler (Phase 8 PR-2).
 *
 * Next.js 14 app-router pattern: NextAuth v4.24's `NextAuth(authOptions)`
 * returns a handler that natively accepts the App Router `Request` and
 * returns a `Response`. We export it as both `GET` and `POST` so every
 * NextAuth surface (signin, signout, callback, csrf, etc.) lands here.
 *
 * Magic-link rate limiting is layered inside the `/api/auth/signin/email`
 * POST path via a thin wrapper — NextAuth v4's options shape doesn't
 * expose a pre-send hook, so we intercept the request before delegating.
 *
 * SECURITY: this handler must be the ONLY route serving anything under
 * `/api/auth/*`. The middleware deliberately excludes this prefix from
 * the auth-guard matcher; if something else mounted here, it would run
 * un-gated. Keep the matcher and this file in lockstep.
 *
 * Rate-limit response semantics:
 *   - Returns 429 (not 200) so log analytics + alerts can distinguish
 *     throttled signin from accepted signin.
 *   - Body matches NextAuth's `{ url: "..." }` JSON shape so the
 *     standard browser form-submit redirect path still works in the
 *     happy case for non-XHR clients.
 *   - Body-parse failures on this specific path are blocked (429 with a
 *     malformed-request label) rather than passed through — otherwise a
 *     deliberately mis-encoded body bypasses the limiter.
 */

import NextAuth from "next-auth";
import { NextResponse } from "next/server";

import { authOptions, validateAuthEnv } from "@/lib/auth/options";
import { allowMagicLink } from "@/lib/rate-limit/magic-link";

// NextAuth v4 detects App Router mode by checking `req instanceof Request`.
// It falls back to Pages Router mode (and crashes on `req.query.nextauth`)
// if it doesn't also receive the route context. Pass context explicitly so
// NextAuth can resolve the action from params.nextauth rather than req.query.
type NextAuthContext = { params: { nextauth: string[] } };

const nextAuthHandler = NextAuth(authOptions) as unknown as (
  req: Request,
  ctx: NextAuthContext,
) => Promise<Response>;

interface EmailExtractResult {
  email: string | null;
  parseFailed: boolean;
}

async function extractSigninEmail(req: Request): Promise<EmailExtractResult> {
  const contentType = req.headers.get("content-type") ?? "";
  try {
    if (contentType.includes("application/x-www-form-urlencoded")) {
      const text = await req.text();
      const params = new URLSearchParams(text);
      return { email: params.get("email"), parseFailed: false };
    }
    if (contentType.includes("application/json")) {
      const json: unknown = await req.json();
      if (
        typeof json === "object" &&
        json !== null &&
        "email" in json &&
        typeof (json as { email: unknown }).email === "string"
      ) {
        return { email: (json as { email: string }).email, parseFailed: false };
      }
      return { email: null, parseFailed: false };
    }
    // Any other content-type on the signin/email path is suspicious — we
    // refuse rather than pass through unrate-limited.
    return { email: null, parseFailed: true };
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn(
      "[portal /api/auth/signin/email] body parse failed; blocking as rate-limit candidate.",
      err,
    );
    return { email: null, parseFailed: true };
  }
}

function rateLimitResponse(reason: "rate_limited" | "malformed"): Response {
  const errorParam = reason === "rate_limited" ? "EmailSignin" : "Verification";
  return NextResponse.json(
    {
      url:
        (process.env.NEXTAUTH_URL ?? "") +
        `/api/auth/error?error=${errorParam}`,
    },
    { status: 429 },
  );
}

async function dispatch(req: Request, ctx: NextAuthContext): Promise<Response> {
  // Lazy startup validation — idempotent after the first call. Runs
  // inside the request path (not at module load) so `next build`'s
  // page-data collection doesn't trip on missing envs.
  const missingEnv = validateAuthEnv();
  if (missingEnv.length > 0) {
    // Fail loudly + cleanly rather than proceeding into an opaque NextAuth
    // JWT or nodemailer SMTP error when a required secret is unset.
    return NextResponse.json(
      {
        error: "server_misconfigured",
        detail: `auth not configured: missing ${missingEnv.join(", ")}`,
      },
      { status: 500 },
    );
  }

  const url = new URL(req.url);
  const isSigninEmail =
    url.pathname.endsWith("/signin/email") && req.method === "POST";

  if (!isSigninEmail) {
    return nextAuthHandler(req, ctx);
  }

  // Clone before reading body — NextAuth's downstream handler needs to
  // read it too.
  const cloned = req.clone();
  const { email, parseFailed } = await extractSigninEmail(cloned);

  if (parseFailed) {
    return rateLimitResponse("malformed");
  }
  if (email && !allowMagicLink(email)) {
    return rateLimitResponse("rate_limited");
  }

  return nextAuthHandler(req, ctx);
}

export async function GET(
  req: Request,
  ctx: NextAuthContext,
): Promise<Response> {
  return dispatch(req, ctx);
}

export async function POST(
  req: Request,
  ctx: NextAuthContext,
): Promise<Response> {
  return dispatch(req, ctx);
}
