/**
 * Active tenants tile (System Health dashboard 2026-06-09).
 *
 * Active tenant count (24 h message activity) vs total provisioned,
 * sourced from tenants + messages tables via the health enrichment query.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatInt } from "@/lib/format";
import type { ActiveTenants } from "@/lib/types/health";

interface Props {
  data: ActiveTenants | null;
}

export function ActiveTenantsTile({ data }: Props) {
  if (!data) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            Active Tenants
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground">Unavailable</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold text-muted-foreground">
          Active Tenants
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="mb-4">
          <span className="text-3xl font-bold tabular-nums">
            {formatInt(data.active_24h)}
          </span>
          <span className="ml-2 text-sm text-muted-foreground">
            Active (24 h)
          </span>
        </div>
        <div className="flex flex-col gap-0.5">
          <span className="text-xl font-bold tabular-nums">
            {formatInt(data.total_provisioned)}
          </span>
          <span className="text-xs text-muted-foreground">provisioned</span>
        </div>
        <p className="mt-3 text-xs text-muted-foreground/60">
          {formatInt(data.active_24h)} of {formatInt(data.total_provisioned)}{" "}
          tenants active
        </p>
      </CardContent>
    </Card>
  );
}
