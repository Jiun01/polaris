/**
 * Tenant create wizard page (Phase 9 PR-3).
 *
 * Server Component wraps the client wizard. Server-side existence
 * because we may want to preload sales-quote data via a `?quote=` query
 * param in PR-9 — placeholder is fine for PR-3.
 */

import { CreateTenantWizard } from "@/components/tenants/CreateWizard";

export default function NewTenantPage() {
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">New tenant</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Create a customer tenant and dispatch the portal magic-link.
        </p>
      </div>
      <CreateTenantWizard />
    </div>
  );
}
