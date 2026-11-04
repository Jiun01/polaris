/**
 * NextAuth catch-all route handler for admin (Phase 9 PR-2).
 *
 * Mirrors apps/portal/app/api/auth/[...nextauth]/route.ts. Adds the
 * IP-bind compensating control per ADR-030 Am.2026-05-20:
 *
 *   1. Extract source IP from forwarded headers (lib/auth/request-ip.ts).
 *   2. For POST /api/auth/signin/email: record the IP into the IP-bind
 *      LRU keyed by the request's email (lib/auth/ip-bind.ts).
 *   3. For ALL requests: wrap the NextAuth call in an AsyncLocalStorage
 *      scope so the jwt callback can read the CURRENT request's IP
 *      back when validating a click.
 *
 * SECURITY: This handler must be the ONLY route serving anything under
 * `/api/auth/*`. The middleware deliberately excludes this prefix from
 * the auth-guard matcher; if something else mounted here, it would run
 * un-gated. Keep the matcher and this file in lockstep.
 *
 * Rate-limit response semantics:
 *
 *   - Returns 429 (not 200) so log analytics + alerts can distinguish
 *     throttled signin from accepted signin.
 *   - Body matches NextAuth's `{ url: "..." }` JSON shape so the
 *     standard browser form-submit redirect path still works in the
 *     happy case for non-XHR clients.
 *   - Body-parse failures on this specific path are blocked (429 with
 *     a malformed-request label) rather than passed through —
 *     otherwise a deliberately mis-encoded body could bypass the
 *     limiter.
 */

import NextAuth from "next-auth";
import { NextResponse } from "next/server";

import {
  authOptions,
  syncAuthEnvAliases,
  validateAuthEnv,
} from "@/lib/auth/options";
import { recordBindForRequest } from "@/lib/auth/ip-bind";
import { extractClickIp, withRequestIp } from "@/lib/auth/request-ip";
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
    return { email: null, parseFailed: true };
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn(
      "[admin /api/auth/signin/email] body parse failed; blocking as rate-limit candidate.",
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
        (process.env.ADMIN_NEXTAUTH_URL ?? process.env.NEXTAUTH_URL ?? "") +
        `/api/auth/error?error=${errorParam}`,
    },
    { status: 429 },
  );
}

async function dispatch(req: Request, ctx: NextAuthContext): Promise<Response> {
  // Bridge ADMIN_* env names onto the NEXTAUTH_* names NextAuth's library
  // reads directly. Idempotent + cheap; runs first so getToken et al
  // resolve correctly downstream.
  syncAuthEnvAliases();
  const missingEnv = validateAuthEnv();
  if (missingEnv.length > 0) {
    // Fail loudly + cleanly instead of proceeding into an opaque NextAuth JWT
    // or nodemailer SMTP error when a required secret is unset.
    return NextResponse.json(
      {
        error: "server_misconfigured",
        detail: `auth not configured: missing ${missingEnv.join(", ")}`,
      },
      { status: 500 },
    );
  }

  const url = new URL(req.url);
  const clickIp = extractClickIp(req);
  const isSigninEmail =
    url.pathname.endsWith("/signin/email") && req.method === "POST";

  if (isSigninEmail) {
    // Clone before reading body — NextAuth's downstream handler also
    // reads the body.
    const cloned = req.clone();
    const { email, parseFailed } = await extractSigninEmail(cloned);

    if (parseFailed) {
      return rateLimitResponse("malformed");
    }
    if (email && !allowMagicLink(email)) {
      return rateLimitResponse("rate_limited");
    }
    if (email && clickIp) {
      // Record the IP-bind BEFORE delegating to NextAuth. Even if
      // NextAuth fails later (SMTP outage etc.), the bind is recorded
      // so a retry-with-same-link from the same /24 still succeeds.
      recordBindForRequest(email, clickIp);
    }
  }

  // Wrap NextAuth's invocation in the AsyncLocalStorage scope so any
  // callback (jwt, session, signIn) can read the click IP back via
  // getRequestIpForJwt().
  return withRequestIp(clickIp, () => nextAuthHandler(req, ctx));
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
