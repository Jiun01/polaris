/**
 * Broadcast queue depth tile (Phase 9 PR-6).
 *
 * Shows: pending count, in-flight count, failed-last-hour count.
 * Non-zero failed count triggers an amber warning band.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatInt } from "@/lib/format";
import type { BroadcastQueueDepth } from "@/lib/types/health";

interface Props {
  data: BroadcastQueueDepth | null;
}

function Metric({
  label,
  value,
  warn,
}: {
  label: string;
  value: number;
  warn?: boolean;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <span
        className={`text-xl font-bold tabular-nums ${warn && value > 0 ? "text-amber-600 dark:text-amber-400" : ""}`}
      >
        {formatInt(value)}
      </span>
      <span className="text-xs text-muted-foreground">{label}</span>
    </div>
  );
}

export function BroadcastQueueDepthTile({ data }: Props) {
  if (!data) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            Broadcast Queue
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
          Broadcast Queue
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-3 gap-4">
          <Metric label="Pending" value={data.pending} />
          <Metric label="In-Flight" value={data.in_flight} />
          <Metric label="Failed (1 h)" value={data.failed_last_hour} warn />
        </div>
      </CardContent>
    </Card>
  );
}
