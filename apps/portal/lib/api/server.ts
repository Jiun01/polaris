/**
 * Server-side Polaris API wrapper (Phase 8 PR-3).
 *
 * Used inside Server Components, Route Handlers, and Server Actions.
 * Reads the NextAuth JWT server-side via `getServerSession()` and
 * forwards relevant claims to the Polaris API. Does NOT use cookies
 * directly because Server Components can't rely on the browser to
 * forward them — we need to construct the outbound request from the
 * server's network namespace.
 *
 * For deployments where the portal container reaches the API on a
 * private hostname (Docker Compose network), set
 * `POLARIS_API_INTERNAL_URL` — it takes precedence over the public
 * `POLARIS_API_BASE_URL`.
 */

import "server-only";

import { createHmac } from "crypto";

import { cookies } from "next/headers";
import { getServerSession } from "next-auth";

import { authOptions } from "@/lib/auth/options";
import { polarisErrorFromResponse } from "@/lib/api/errors";

/**
 * E2E-only server-side fault injection. Server Components fetch on the server,
 * so Playwright's `page.route()` can't intercept them; this hook lets E2E tests
 * force a fault by setting an `e2e_fault` cookie before navigation.
 *
 * STRICTLY gated on `POLARIS_E2E_INTERNAL_SECRET` — production never sets it,
 * so this is inert dead code there (the cookie is simply ignored).
 *
 * Cookie value (URL-encoded JSON array): each directive matches by path
 * substring and either forces an HTTP error (`status` >= 400 → the page's
 * error path) or returns a canned body (`body`, for empty/populated states):
 *   [{ "match": "dashboard/summary", "status": 503 },
 *    { "match": "audience", "body": { "items": [] } }]
 */
interface E2EFaultDirective {
  match: string;
  status?: number;
  body?: unknown;
  message?: string;
}

async function readE2EFault(path: string): Promise<E2EFaultDirective | null> {
  if (!process.env.POLARIS_E2E_INTERNAL_SECRET) return null;
  let raw: string | undefined;
  try {
    raw = cookies().get("e2e_fault")?.value;
  } catch {
    return null; // not in a request scope
  }
  if (!raw) return null;
  try {
    const directives = JSON.parse(
      decodeURIComponent(raw),
    ) as E2EFaultDirective[];
    return directives.find((d) => d.match && path.includes(d.match)) ?? null;
  } catch {
    return null;
  }
}

function b64url(buf: Buffer): string {
  return buf
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

/**
 * Mint a short-lived (60 s) HS256 JWT for a single Polaris API call.
 * Uses the same JWT_SECRET / JWT_ISSUER the FastAPI backend reads, so
 * get_tenant_context() can decode and verify it.
 */
export function mintApiJwt(
  userId: string,
  tenantId: string,
  role: string,
): string {
  const secret = process.env.JWT_SECRET ?? "";
  const issuer = process.env.JWT_ISSUER ?? "polaris-portal";
  const now = Math.floor(Date.now() / 1000);
  const hdr = b64url(Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })));
  const pay = b64url(
    Buffer.from(
      JSON.stringify({
        sub: userId,
        tenant_id: tenantId,
        role,
        iss: issuer,
        iat: now,
        exp: now + 60,
      }),
    ),
  );
  const sig = b64url(
    createHmac("sha256", secret).update(`${hdr}.${pay}`).digest(),
  );
  return `${hdr}.${pay}.${sig}`;
}

function baseUrl(): string {
  return (
    process.env.POLARIS_API_INTERNAL_URL ??
    process.env.POLARIS_API_BASE_URL ??
    ""
  );
}

interface ServerFetchOptions extends Omit<RequestInit, "headers"> {
  headers?: Record<string, string>;
  unauthenticated?: boolean;
  /**
   * Next.js fetch-cache directive. Default `"no-store"` so server
   * components don't accidentally cache tenant-scoped data across
   * requests. Callers can override for surfaces where caching is safe.
   */
  cache?: RequestCache;
}

/**
 * Server-side analogue of apiFetch. Returns `T | undefined` because
 * a 204 No Content response carries no body. Endpoints that can return
 * 204 should be called with `T = void` so consumers handle the
 * undefined explicitly.
 */
export async function apiFetchServer<T>(
  path: string,
  init: ServerFetchOptions = {},
): Promise<T | undefined> {
  // E2E fault injection (no-op unless POLARIS_E2E_INTERNAL_SECRET is set).
  const fault = await readE2EFault(path);
  if (fault) {
    if (typeof fault.status === "number" && fault.status >= 400) {
      throw await polarisErrorFromResponse(
        new Response(
          JSON.stringify({
            error: {
              code: "e2e_fault",
              message: fault.message ?? "E2E injected fault",
            },
          }),
          {
            status: fault.status,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );
    }
    return fault.body as T;
  }

  const url = `${baseUrl()}${path}`;
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(init.body ? { "Content-Type": "application/json" } : {}),
    ...init.headers,
  };

  if (!init.unauthenticated) {
    const session = await getServerSession(authOptions);
    if (!session?.user?.id || !session.tenant_id) {
      // No session for an authenticated call — fail BEFORE the fetch rather
      // than silently forwarding without a Bearer (which FastAPI rejects as a
      // generic 401, indistinguishable from a bad token). A distinct
      // `no_session` code lets callers redirect to /login instead of rendering
      // an empty-default fallback.
      throw await polarisErrorFromResponse(
        new Response(
          JSON.stringify({
            error: {
              code: "no_session",
              message: "No authenticated session for a tenant-scoped request",
            },
          }),
          { status: 401, headers: { "Content-Type": "application/json" } },
        ),
      );
    }
    const token = mintApiJwt(
      session.user.id,
      session.tenant_id,
      session.role ?? "admin",
    );
    headers["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetch(url, {
    ...init,
    cache: init.cache ?? "no-store",
    headers,
  });

  if (!res.ok) {
    throw await polarisErrorFromResponse(res);
  }

  if (res.status === 204) {
    return undefined;
  }

  return (await res.json()) as T;
}
