/**
 * First-run onboarding page (Phase 8 PR-5).
 *
 * Server Component that mounts the client-side wizard. Middleware
 * (apps/portal/middleware.ts) redirects fresh tenants here from
 * /dashboard when both criteria hold:
 *   - tenant_channels rowcount == 0
 *   - no published flow exists
 *
 * Once either condition becomes false, middleware stops redirecting.
 * The tenant can still navigate to /onboarding intentionally — the
 * page renders regardless of completion state, treating it as a
 * reference rather than a gating wall.
 */

import { OnboardingWizard } from "@/components/onboarding/OnboardingWizard";

export default function OnboardingPage() {
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight">
          Welcome to Polaris
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Three steps to get your bot live. Skip anything you want to come back
          to later.
        </p>
      </header>
      <OnboardingWizard />
    </div>
  );
}
