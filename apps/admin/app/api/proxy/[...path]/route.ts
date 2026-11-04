/**
 * Same-origin proxy to the Polaris FastAPI (Phase 9 E2E fix).
 *
 * Browser-side adminFetch calls `/api/proxy/<fastapi-path>` (same-origin,
 * no CORS needed). This handler mints a platform-admin JWT server-side
 * and forwards the request to FastAPI via adminServerFetch.
 *
 * Why: FastAPI lives on a different origin (localhost:8000 / api.localhost)
 * so direct browser→API calls are blocked by CORS and have no Bearer token.
 * Routing through Next.js eliminates both problems.
 */

import { type NextRequest, NextResponse } from "next/server";

import { ApiError } from "@/lib/api/error";
import { adminServerFetch } from "@/lib/api/server";

type RouteContext = { params: { path: string[] } };

// The admin app only ever proxies these FastAPI prefixes. Anything else
// (/internal/*, /v1/webhooks/*, /api/tenant/*, …) must not be reachable through
// an admin session even if a future route relaxes its server-side auth dep.
const ALLOWED_PROXY_PREFIXES = ["/api/admin/", "/api/billing/", "/api/quotes/"];

async function handler(req: NextRequest, { params }: RouteContext) {
  const apiPath = "/" + params.path.join("/");
  if (
    apiPath.includes("..") ||
    !ALLOWED_PROXY_PREFIXES.some((p) => apiPath.startsWith(p))
  ) {
    return NextResponse.json(
      { error: { code: "forbidden_path", message: "Proxy path not allowed" } },
      { status: 403 },
    );
  }
  const { searchParams } = req.nextUrl;
  const qs = searchParams.toString();
  const fullPath = qs ? `${apiPath}?${qs}` : apiPath;

  let body: unknown;
  if (req.method !== "GET" && req.method !== "HEAD") {
    const text = await req.text();
    if (text) {
      try {
        // Note: adminServerFetch re-serialises with JSON.stringify, so the
        // round-trip is lossless for JSON (modulo >2^53 integers, which the
        // admin API does not use). A non-JSON body would be double-encoded —
        // reject it rather than forward a corrupted payload upstream.
        body = JSON.parse(text);
      } catch {
        return NextResponse.json(
          {
            error: {
              code: "invalid_json_body",
              message: "Request body must be valid JSON",
            },
          },
          { status: 400 },
        );
      }
    }
  }

  try {
    // Capture Set-Cookie headers from the FastAPI response so we can forward
    // them to the browser. Without this the impersonation cookie (and any
    // other cookie the API sets) would be silently dropped.
    const rawCookies: string[] = [];
    // Preserve the upstream status (201 Created / 202 Accepted / 206 etc.)
    // instead of flattening every success to 200. adminServerFetch consumes
    // the body, so we capture the status from the raw response here, before it
    // is read. (Non-2xx upstreams throw ApiError and are handled in catch.)
    let upstreamStatus = 200;
    const data = await adminServerFetch(fullPath, {
      method: req.method,
      ...(body !== undefined ? { body } : {}),
      onRawResponse: (res) => {
        upstreamStatus = res.status;
        // Node 20+ Headers API exposes getSetCookie() for multi-value headers.
        const raw =
          typeof (res.headers as { getSetCookie?: () => string[] })
            .getSetCookie === "function"
            ? (res.headers as { getSetCookie: () => string[] }).getSetCookie()
            : (res.headers.get("set-cookie")?.split(/,(?=\s*\w+=)/) ?? []);
        rawCookies.push(...raw);
      },
    });

    // Build the response body, preserving the upstream status.
    let status = upstreamStatus;
    let responseBody: string | null = null;
    const responseHeaders = new Headers({ "content-type": "application/json" });

    if (data === undefined) {
      // Upstream returned no body (e.g. 204 No Content) — keep its status
      // (falls back to 204 if the upstream somehow reported 200 with no body).
      status = upstreamStatus === 200 ? 204 : upstreamStatus;
      responseBody = null;
      responseHeaders.delete("content-type");
    } else {
      responseBody = JSON.stringify(data);
    }

    // Forward every Set-Cookie header from the FastAPI response verbatim.
    // Using native Headers.append preserves the Domain attribute exactly.
    for (const raw of rawCookies) {
      if (raw) responseHeaders.append("set-cookie", raw);
    }

    return new NextResponse(responseBody, { status, headers: responseHeaders });
  } catch (err) {
    if (err instanceof ApiError) {
      return NextResponse.json(
        err.envelope ?? { error: { code: "api_error", message: err.message } },
        { status: err.status },
      );
    }
    console.error("[proxy] Unexpected error:", err);
    return NextResponse.json(
      { error: { code: "proxy_error", message: "Internal proxy error" } },
      { status: 502 },
    );
  }
}

export {
  handler as DELETE,
  handler as GET,
  handler as PATCH,
  handler as POST,
  handler as PUT,
};
