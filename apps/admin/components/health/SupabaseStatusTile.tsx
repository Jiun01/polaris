/**
 * Supabase status tile (Phase 9 PR-6).
 *
 * Renders a green/red indicator + DB latency ms (last probe).
 * If the probe is degraded, shows the error message text — not
 * just a colour change.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { formatMyt } from "@/lib/format";
import type { SupabaseStatus } from "@/lib/types/health";

interface Props {
  data: SupabaseStatus | null;
}

export function SupabaseStatusTile({ data }: Props) {
  if (!data) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            Supabase
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
            Supabase
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
        {data.db_latency_ms !== null && data.db_latency_ms !== undefined ? (
          <p className="text-2xl font-bold tabular-nums">
            {data.db_latency_ms}
            <span className="ml-1 text-sm font-normal text-muted-foreground">
              ms
            </span>
          </p>
        ) : (
          <p className="text-2xl font-bold text-muted-foreground">—</p>
        )}
        <p className="text-xs text-muted-foreground">DB latency (last probe)</p>
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
