/**
 * Portal impersonation banner (Phase 9 PR-5).
 *
 * Server Component. Reads the x-polaris-impersonating request header
 * (set by the portal middleware when the pol_impersonation cookie is
 * present) to determine if the current session is being impersonated.
 *
 * When active, renders a full-width amber warning bar so the tenant
 * user (or the impersonating admin) can immediately see that the session
 * is operating under admin observation.
 *
 * Security note: this is a DISPLAY-ONLY banner. The cookie is HttpOnly
 * and cannot be read by portal JavaScript. This component reads the
 * header forwarded by the middleware, which is trusted server-side
 * infrastructure. The banner does NOT grant or restrict any permissions.
 */

import { headers } from "next/headers";

const IMPERSONATING_HEADER = "x-polaris-impersonating";

export function ImpersonationBanner() {
  const headerStore = headers();
  const impersonating = headerStore.get(IMPERSONATING_HEADER);

  if (!impersonating) return null;

  return (
    <div
      data-testid="impersonation-banner"
      role="alert"
      aria-live="polite"
      className="flex items-center justify-center gap-2 bg-amber-500 px-4 py-2 text-center text-xs font-medium text-white dark:bg-amber-600"
    >
      <span aria-hidden>&#x26A0;</span>
      This portal session is being observed by a Polaris platform admin. All
      actions are audited. Close this tab when done.
      <span aria-hidden>&#x26A0;</span>
    </div>
  );
}
