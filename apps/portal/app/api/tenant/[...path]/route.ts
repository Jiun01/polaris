/**
 * Polaris API proxy — catch-all route handler (Phase 8 PR-12).
 *
 * All client-side `apiFetch("/api/tenant/...")` calls land here.
 * The handler:
 *   1. Reads the NextAuth session server-side.
 *   2. Mints a short-lived HS256 JWT with `mintApiJwt` (same secret +
 *      issuer as the FastAPI backend expects).
 *   3. Forwards the request verbatim — method, body, query string — to
 *      the real Polaris API at POLARIS_API_INTERNAL_URL (Docker Compose
 *      private network) or POLARIS_API_BASE_URL (public fallback).
 *   4. Streams the upstream response body back to the browser unchanged,
 *      preserving the status code.
 *
 * Header policy:
 *   - Forwarded to upstream: Accept, Content-Type (when present), and
 *     the minted Authorization Bearer token.
 *   - NOT forwarded: Host, Cookie, X-Forwarded-* — these are either
 *     meaningless to the API container or actively harmful (cookie
 *     leakage across trust boundaries).
 *
 * Error semantics:
 *   - Session missing → 401 { detail: "unauthenticated" }
 *   - Upstream non-OK → status + body proxied as-is so the client's
 *     `polarisErrorFromResponse` can parse it normally.
 */

import { getServerSession } from "next-auth";
import { NextResponse } from "next/server";

import { authOptions } from "@/lib/auth/options";
import { mintApiJwt } from "@/lib/api/server";

// Headers we must NOT forward to the upstream API.
const BLOCKED_REQUEST_HEADERS = new Set([
  "host",
  "cookie",
  // The server mints the Authorization Bearer below; a browser-supplied
  // Authorization header must never override it, or a session holder could
  // substitute an arbitrary token. The minted token is always authoritative.
  "authorization",
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-port",
  "x-forwarded-proto",
  "x-forwarded-scheme",
  "x-real-ip",
]);

function upstreamBase(): string {
  return (
    process.env.POLARIS_API_INTERNAL_URL ??
    process.env.POLARIS_API_BASE_URL ??
    ""
  );
}

/**
 * Strip the Next.js prefix (`/api/tenant`) and reconstruct the full
 * upstream URL, preserving any query string on the incoming request.
 *
 * e.g.  /api/tenant/chatbots?page=2  →  http://api:8000/api/tenant/chatbots?page=2
 */
function buildUpstreamUrl(req: Request, segments: string[]): string {
  const tail = segments.join("/");
  const incomingUrl = new URL(req.url);
  const qs = incomingUrl.search; // includes the "?" or is ""
  return `${upstreamBase()}/api/tenant/${tail}${qs}`;
}

type RouteContext = { params: { path: string[] } };

async function proxyRequest(
  req: Request,
  ctx: RouteContext,
): Promise<Response> {
  // 1. Authenticate.
  const session = await getServerSession(authOptions);
  if (!session?.user?.id || !session.tenant_id) {
    return NextResponse.json({ detail: "unauthenticated" }, { status: 401 });
  }

  // 2. Mint a short-lived HS256 JWT.
  const token = mintApiJwt(
    session.user.id,
    session.tenant_id,
    session.role ?? "admin",
  );

  // 3. Build the outbound header set.
  const outHeaders: Record<string, string> = {
    Authorization: `Bearer ${token}`,
    Accept: "application/json",
  };

  // Forward safe inbound headers (content-type, etc.) — skip blocked ones.
  req.headers.forEach((value, key) => {
    if (!BLOCKED_REQUEST_HEADERS.has(key.toLowerCase())) {
      outHeaders[key] = value;
    }
  });

  // 4. Build the upstream URL. Guard against a misconfigured deploy: with
  //    both upstream env vars unset, upstreamBase() is "" and the URL would
  //    resolve relative — silently self-routing into the Next.js server.
  //    Fail loudly with a clear 500 instead.
  if (!upstreamBase()) {
    return NextResponse.json(
      {
        detail:
          "proxy_misconfigured: POLARIS_API_INTERNAL_URL/POLARIS_API_BASE_URL unset",
      },
      { status: 500 },
    );
  }
  const upstreamUrl = buildUpstreamUrl(req, ctx.params.path);

  // 5. Determine whether to forward a body.  GET/HEAD must not send one.
  const hasBody = req.method !== "GET" && req.method !== "HEAD";

  // 6. Read the body upfront so we can replay it through a redirect.
  //    undici detaches the ArrayBuffer when it auto-follows a redirect, so we
  //    use redirect:"manual" for body-bearing requests and replay manually.
  const bodyBuf = hasBody ? await req.arrayBuffer() : undefined;

  const upstream = await fetch(upstreamUrl, {
    method: req.method,
    headers: outHeaders,
    body: bodyBuf,
    cache: "no-store",
    // Prevent undici from detaching the body buffer on a 307/308 replay.
    redirect: hasBody ? "manual" : "follow",
  });

  // 7. Handle FastAPI trailing-slash 307/308 redirects for body-bearing requests.
  //    FastAPI (redirect_slashes=True default) returns 307 when a POST hits
  //    /api/tenant/flows instead of /api/tenant/flows/ — we replay manually so
  //    the body buffer stays valid.
  if (hasBody && (upstream.status === 307 || upstream.status === 308)) {
    const location = upstream.headers.get("location");
    if (location) {
      const redirectTarget = location.startsWith("http")
        ? location
        : `${upstreamBase()}${location}`;
      // Only follow redirects that stay within the tenant API surface. The
      // sole legitimate case is FastAPI's trailing-slash 307 (same path + "/").
      // Refuse anything else so a misbehaving/forged Location can't make us
      // replay the request — with the user's minted JWT — to an internal path.
      let redirectPath: string;
      try {
        redirectPath = new URL(redirectTarget).pathname;
      } catch {
        return NextResponse.json(
          { detail: "bad_gateway: invalid redirect location" },
          { status: 502 },
        );
      }
      if (!redirectPath.startsWith("/api/tenant/")) {
        return NextResponse.json(
          { detail: "bad_gateway: upstream redirect out of scope" },
          { status: 502 },
        );
      }
      const retried = await fetch(redirectTarget, {
        method: req.method,
        headers: outHeaders,
        body: bodyBuf,
        cache: "no-store",
      });
      return new Response(retried.body, {
        status: retried.status,
        statusText: retried.statusText,
        headers: {
          "Content-Type":
            retried.headers.get("Content-Type") ?? "application/json",
        },
      });
    }
  }

  // 8. Stream the response body back to the browser, preserving status.
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: {
      "Content-Type":
        upstream.headers.get("Content-Type") ?? "application/json",
    },
  });
}

export async function GET(req: Request, ctx: RouteContext): Promise<Response> {
  return proxyRequest(req, ctx);
}
export async function POST(req: Request, ctx: RouteContext): Promise<Response> {
  return proxyRequest(req, ctx);
}
export async function PUT(req: Request, ctx: RouteContext): Promise<Response> {
  return proxyRequest(req, ctx);
}
export async function PATCH(
  req: Request,
  ctx: RouteContext,
): Promise<Response> {
  return proxyRequest(req, ctx);
}
export async function DELETE(
  req: Request,
  ctx: RouteContext,
): Promise<Response> {
  return proxyRequest(req, ctx);
}
