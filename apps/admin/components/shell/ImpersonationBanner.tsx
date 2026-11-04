/**
 * Admin-side impersonation banner (Phase 9 PR-5).
 *
 * Shown in the admin shell when an impersonation session is active
 * (i.e. the admin opened a portal tab). Server component — reads the
 * pol_impersonation cookie from the incoming request headers.
 *
 * Provides a "Stop session" button that calls DELETE /api/admin/impersonate
 * and clears the cookie.
 *
 * Note: this banner sits in the admin shell. The portal's own
 * ImpersonationBanner (apps/portal/components/shell/ImpersonationBanner.tsx)
 * reads the same cookie to surface the "You are being impersonated" warning
 * to the tenant user view.
 */

import { cookies } from "next/headers";
import { StopImpersonationButton } from "./StopImpersonationButton";

const COOKIE_NAME = "pol_impersonation";

export async function ImpersonationBanner() {
  const cookieStore = cookies();
  const cookie = cookieStore.get(COOKIE_NAME);
  if (!cookie?.value) return null;

  return (
    <div
      data-testid="impersonation-banner"
      role="alert"
      className="flex items-center justify-between gap-4 bg-amber-700 px-4 py-2 text-xs font-medium text-white dark:bg-amber-800"
    >
      <span>
        Impersonation session active — portal tab is running as a tenant user.
        All actions are audited.
      </span>
      <StopImpersonationButton />
    </div>
  );
}
