/**
 * Channels — read-only platform-wide inventory (Phase 9 PR-3).
 *
 * Aggregates tenant_channels across the platform: count by kind
 * (whatsapp / instagram / messenger), connected vs disconnected, MM
 * Lite onboarding status (per ADR-034). Admin observes only —
 * per-tenant channel rotation lives in the portal Channels screen
 * (Phase 8 PR-5).
 *
 * PR-3 ships the shell; backend GET /api/admin/channels lands in a
 * later PR alongside the System Health surface (PR-6).
 */

import { Card } from "@/components/ui/card";

export default function ChannelsPage() {
  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Channels</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Platform-wide channel inventory. Per-tenant rotation lives in the
          portal.
        </p>
      </div>
      <Card className="border-dashed p-6 text-sm text-muted-foreground">
        Aggregate channel inventory surfaces alongside the System Health page
        (PR-6). For now, per-tenant channels render in Tenants → detail →
        Channels tab.
      </Card>
    </div>
  );
}
