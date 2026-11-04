/**
 * Browser-side fetch wrapper for the admin app (Phase 9 PR-3).
 *
 * Routes all Polaris API calls through the Next.js same-origin proxy at
 * /api/proxy/<fastapi-path>. The proxy (app/api/proxy/[...path]/route.ts)
 * mints a platform-admin JWT server-side and forwards the request to
 * FastAPI via adminServerFetch.
 *
 * Why the proxy: FastAPI lives on a different origin (localhost:8000 or
 * api.localhost) so direct browser→API calls are blocked by CORS and
 * cannot carry a Bearer token. Same-origin proxy eliminates both problems.
 */

import { apiErrorFromResponse } from "@/lib/api/error";

// All browser calls go to the Next.js proxy (same-origin).
const PROXY_BASE = "/api/proxy";

export interface FetchOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
}

export async function adminFetch<T>(
  path: string,
  init?: FetchOptions,
): Promise<T | undefined> {
  const url = `${PROXY_BASE}${path}`;
  const headers = new Headers(init?.headers ?? {});
  headers.set("Accept", "application/json");
  let body: BodyInit | undefined;
  if (init?.body !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.body);
  }

  const res = await fetch(url, {
    ...init,
    headers,
    body,
  });

  if (!res.ok) {
    throw await apiErrorFromResponse(res);
  }
  if (res.status === 204) {
    return undefined;
  }
  return (await res.json()) as T;
}
