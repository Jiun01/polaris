/**
 * Service connectivity tile (System Health enrichment 2026-06-09).
 *
 * Reusable — rendered once for Chatwoot, once for EA. Shows a
 * Healthy/Degraded badge, round-trip latency, and the error message
 * when degraded.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { formatMyt } from "@/lib/format";
import type { ServiceConnectivity } from "@/lib/types/health";

interface Props {
  title: string;
  data: ServiceConnectivity | null;
}

export function ServiceConnectivityTile({ title, data }: Props) {
  if (!data) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            {title}
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
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            {title}
          </CardTitle>
          <Badge
            variant={data.healthy ? "default" : "destructive"}
            className={
              data.healthy ? "bg-green-500 hover:bg-green-600" : undefined
            }
          >
            {data.healthy ? "Healthy" : "Degraded"}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-1">
        {data.latency_ms !== null && data.latency_ms !== undefined ? (
          <p className="text-2xl font-bold tabular-nums">
            {data.latency_ms}
            <span className="ml-1 text-sm font-normal text-muted-foreground">
              ms
            </span>
          </p>
        ) : (
          <p className="text-2xl font-bold text-muted-foreground">—</p>
        )}
        {data.error && (
          <p className="mt-2 rounded-md bg-destructive/10 px-2 py-1 text-xs text-destructive">
            {data.error}
          </p>
        )}
        <p className="text-xs text-muted-foreground/60">
          Checked {formatMyt(data.last_checked_at)}
        </p>
      </CardContent>
    </Card>
  );
}
