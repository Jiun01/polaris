/**
 * Server-side fetch wrapper for the admin app (Phase 9 PR-3).
 *
 * Used inside Server Components, Route Handlers, and Server Actions —
 * anywhere that runs in the Node process, NOT in the browser. Reads
 * the NextAuth JWT server-side via getServerSession and forwards it
 * as an Authorization header to the Polaris API.
 *
 * Why this exists separate from lib/api/client.ts:
 *
 *   - The browser client wrapper relies on `credentials: 'include'` so
 *     the session cookie travels with the request. Server-side fetches
 *     don't get that cookie automatically — we have to either copy the
 *     incoming cookie header or mint a Bearer from the session JWT.
 *
 *   - Server-side calls need to use POLARIS_API_INTERNAL_URL (when set)
 *     to talk to the API across the Docker network, not the public URL.
 */

import { createHmac } from "crypto";

import { getServerSession } from "next-auth/next";

import { cookies } from "next/headers";

import { authOptions, syncAuthEnvAliases } from "@/lib/auth/options";
import { ApiError, apiErrorFromResponse } from "@/lib/api/error";

/**
 * E2E-only server-side fault injection (mirrors the portal). Server Components
 * fetch on the server, so Playwright page.route() can't intercept them; E2E
 * tests set an `e2e_fault` cookie to force a fault. Gated on
 * POLARIS_E2E_INTERNAL_SECRET — inert in production.
 */
interface E2EFaultDirective {
  match: string;
  status?: number;
  body?: unknown;
  message?: string;
}

function readE2EFault(path: string): E2EFaultDirective | null {
  if (!process.env.POLARIS_E2E_INTERNAL_SECRET) return null;
  let raw: string | undefined;
  try {
    raw = cookies().get("e2e_fault")?.value;
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const ds = JSON.parse(decodeURIComponent(raw)) as E2EFaultDirective[];
    return ds.find((d) => d.match && path.includes(d.match)) ?? null;
  } catch {
    return null;
  }
}

// Promote ADMIN_NEXTAUTH_SECRET → NEXTAUTH_SECRET (and ADMIN_NEXTAUTH_URL →
// NEXTAUTH_URL) so that getServerSession() works in Server Components and
// Route Handlers that import this module, not just in the /api/auth/* handler.
// syncAuthEnvAliases is idempotent — first call wins.
syncAuthEnvAliases();

export const SERVER_BASE_URL =
  process.env.POLARIS_API_INTERNAL_URL ??
  process.env.POLARIS_API_BASE_URL ??
  "http://localhost:8000";

export interface ServerFetchOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  /**
   * Called with the raw fetch Response immediately after it arrives, before
   * the body is consumed. Use this to capture response headers (e.g.
   * Set-Cookie) that must be forwarded to the browser. Must NOT read the body.
   */
  onRawResponse?: (res: Response) => void;
}

/**
 * Mint a short-lived HS256 JWT the Polaris API accepts as a platform-admin
 * bearer token. The API's require_platform_admin dep decodes it with
 * JWT_SECRET and requires role='platform_admin' + exp/iat/iss claims.
 */
function mintAdminJwt(platformAdminId: string): string {
  const header = Buffer.from(
    JSON.stringify({ alg: "HS256", typ: "JWT" }),
  ).toString("base64url");
  const now = Math.floor(Date.now() / 1000);
  const payload = Buffer.from(
    JSON.stringify({
      sub: platformAdminId,
      platform_admin_id: platformAdminId,
      role: "platform_admin",
      iss: process.env.JWT_ISSUER ?? "polaris-portal",
      iat: now,
      exp: now + 300, // 5-minute TTL — fine for SSR page renders
    }),
  ).toString("base64url");
  const secret = process.env.JWT_SECRET ?? "";
  const sig = createHmac("sha256", secret)
    .update(`${header}.${payload}`)
    .digest("base64url");
  return `${header}.${payload}.${sig}`;
}

/**
 * Resolve a bearer token for the API from the current NextAuth session.
 * Mints a fresh short-lived JWT each call (cheap — pure crypto, no I/O).
 */
export async function bearerForApi(): Promise<string | undefined> {
  const session = await getServerSession(authOptions);
  if (!session?.platform_admin_id) return undefined;
  return mintAdminJwt(session.platform_admin_id);
}

export async function adminServerFetch<T>(
  path: string,
  init?: ServerFetchOptions,
): Promise<T | undefined> {
  // E2E fault injection (no-op unless POLARIS_E2E_INTERNAL_SECRET is set).
  const fault = readE2EFault(path);
  if (fault) {
    if (typeof fault.status === "number" && fault.status >= 400) {
      throw new ApiError(fault.status, {
        error: {
          code: "e2e_fault",
          message: fault.message ?? "E2E injected fault",
        },
      });
    }
    return fault.body as T;
  }

  const url = `${SERVER_BASE_URL.replace(/\/$/, "")}${path}`;
  const headers = new Headers(init?.headers ?? {});
  headers.set("Accept", "application/json");

  const bearer = await bearerForApi();
  if (bearer) {
    headers.set("Authorization", `Bearer ${bearer}`);
  }

  let body: BodyInit | undefined;
  if (init?.body !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.body);
  }

  const res = await fetch(url, {
    ...init,
    headers,
    body,
    cache: "no-store",
  });

  // Invoke the caller's hook before consuming the body so it can capture
  // response headers (e.g. Set-Cookie) for forwarding.
  init?.onRawResponse?.(res);

  if (!res.ok) {
    throw await apiErrorFromResponse(res);
  }
  if (res.status === 204) {
    return undefined;
  }
  return (await res.json()) as T;
}
