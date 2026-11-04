/**
 * Redis status tile (Phase 9 PR-6).
 *
 * Renders a green/red indicator + memory used MB from the INFO probe.
 * Error message shown inline when the probe is degraded.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { formatMyt } from "@/lib/format";
import type { RedisStatus } from "@/lib/types/health";

interface Props {
  data: RedisStatus | null;
}

export function RedisStatusTile({ data }: Props) {
  if (!data) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            Redis
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
            Redis
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
        {data.used_memory_mb !== null && data.used_memory_mb !== undefined ? (
          <p className="text-2xl font-bold tabular-nums">
            {data.used_memory_mb}
            <span className="ml-1 text-sm font-normal text-muted-foreground">
              MB
            </span>
          </p>
        ) : (
          <p className="text-2xl font-bold text-muted-foreground">—</p>
        )}
        <p className="text-xs text-muted-foreground">Memory used</p>
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
