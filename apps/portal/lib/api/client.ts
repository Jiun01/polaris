"use client";

/**
 * Client-side Polaris API wrapper (Phase 8 PR-3).
 *
 * Used inside React Client Components. Callers pass relative paths
 * (`/api/tenant/...`); with the default empty base URL these resolve
 * SAME-ORIGIN to the Next.js proxy route (`app/api/tenant/[...path]`),
 * which authenticates the NextAuth session server-side and mints the
 * FastAPI Bearer token. The browser does NOT hold or send the Bearer
 * itself — auth lives entirely in the proxy.
 *
 * For Server Components and Server Actions, use `lib/api/server.ts`
 * instead — it reads the cookie via `next/headers` and mints the
 * Bearer directly.
 */

import { polarisErrorFromResponse } from "@/lib/api/errors";

function baseUrl(): string {
  // Default empty → relative → same-origin → the Next proxy (which owns auth).
  // Only NEXT_PUBLIC_* env vars are inlined into the client bundle by Next.js.
  // We intentionally do NOT fall back to POLARIS_API_BASE_URL here — that would
  // be `undefined` in the browser and silently resolve same-origin.
  const base = process.env.NEXT_PUBLIC_POLARIS_API_BASE_URL ?? "";
  // Footgun guard: pointing this at an absolute cross-origin API URL BYPASSES
  // the proxy, so requests would arrive at FastAPI with no Bearer and 401. The
  // proxy is the only auth carrier for client-side calls — keep this empty (or
  // same-origin) in every environment.
  if (base && /^https?:\/\//i.test(base)) {
    // eslint-disable-next-line no-console
    console.error(
      "[portal] NEXT_PUBLIC_POLARIS_API_BASE_URL is set to a cross-origin URL " +
        "— client calls will bypass the auth proxy and 401. Leave it unset.",
    );
  }
  return base;
}

interface ClientFetchOptions extends Omit<RequestInit, "headers"> {
  /** Extra headers merged with auth + content-type defaults. */
  headers?: Record<string, string>;
}

/**
 * Issue a request against the Polaris API and return its parsed JSON
 * body, typed by the caller. Throws `PolarisApiError` on non-OK
 * responses so consumers can pattern-match on `code`.
 *
 * Returns `T | undefined` because a 204 No Content response carries no
 * body. Endpoints that can return 204 should be called with `T = void`
 * so consumers handle the undefined explicitly.
 */
export async function apiFetch<T>(
  path: string,
  init: ClientFetchOptions = {},
): Promise<T | undefined> {
  const url = `${baseUrl()}${path}`;
  // FormData bodies (e.g. product image upload) must NOT carry an explicit
  // Content-Type — the browser sets multipart/form-data with the boundary.
  const isFormData =
    typeof FormData !== "undefined" && init.body instanceof FormData;
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(init.body && !isFormData ? { "Content-Type": "application/json" } : {}),
    ...init.headers,
  };

  // No Authorization header is attached here: relative `/api/tenant/*` paths
  // hit the same-origin Next proxy, which authenticates the session and mints
  // the Bearer server-side. `credentials: "include"` sends the NextAuth session
  // cookie so the proxy's getServerSession() resolves.
  const res = await fetch(url, {
    ...init,
    credentials: "include",
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
