/**
 * Promotions — read-only platform-wide mirror (Phase 9 PR-3).
 *
 * Tenant-side promotions CRUD ships in the portal (Phase 8 PR-9 +
 * migration 053). The admin app only OBSERVES — operator sees what
 * tenants are running so cost / safety questions can be answered
 * without spelunking RLS. No mutations here.
 *
 * PR-3 renders the shell with a not-yet-wired empty state; full read
 * lands when the backend's GET /api/admin/promotions endpoint is added
 * in a later PR alongside the cost-dashboard work.
 */

import { Card } from "@/components/ui/card";

export default function PromotionsPage() {
  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Promotions</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Read-only platform mirror. Tenant-side authoring lives in the portal.
        </p>
      </div>
      <Card className="border-dashed p-6 text-sm text-muted-foreground">
        Aggregate view coming alongside the cost dashboard (PR-10). For now,
        per-tenant promotions surface inside the Tenants → detail → Promotions
        tab when wired.
      </Card>
    </div>
  );
}
