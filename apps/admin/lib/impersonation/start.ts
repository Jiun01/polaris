/**
 * Client-side impersonation session management (Phase 9 PR-5).
 *
 * startImpersonation: POST /api/admin/impersonate/{tenantId}/{userId}
 *   The API sets the HttpOnly pol_impersonation cookie; JS cannot read it.
 *   Returns the session_id + expires_at from the response body.
 *
 * stopImpersonation: DELETE /api/admin/impersonate
 *   The API clears the cookie server-side.
 */

import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";

export interface ImpersonationSession {
  session_id: string;
  expires_at: string;
}

export async function startImpersonation(
  tenantId: string,
  userId: string,
): Promise<ImpersonationSession> {
  const result = await adminFetch<ImpersonationSession>(
    adminRoutes.impersonation.start(tenantId, userId),
    { method: "POST" },
  );
  if (!result) {
    throw new Error("startImpersonation: empty response from API");
  }
  return result;
}

export async function stopImpersonation(): Promise<void> {
  await adminFetch<void>(adminRoutes.impersonation.stop(), {
    method: "DELETE",
  });
}
