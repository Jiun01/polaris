/**
 * Messaging volume tile (System Health enrichment 2026-06-01).
 *
 * Inbound/outbound message counts (last 24 h, from messages.role) plus
 * metered bot replies month-to-date (tenant_usage).
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatInt, formatMyt } from "@/lib/format";
import type { MessagingVolume } from "@/lib/types/health";

interface Props {
  data: MessagingVolume | null;
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xl font-bold tabular-nums">{formatInt(value)}</span>
      <span className="text-xs text-muted-foreground">{label}</span>
    </div>
  );
}

export function MessagingVolumeTile({ data }: Props) {
  if (!data) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            Messaging Volume
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
          Messaging Volume
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-3 gap-4">
          <Metric label="Inbound (24 h)" value={data.inbound_24h} />
          <Metric label="Outbound (24 h)" value={data.outbound_24h} />
          <Metric label="Bot replies (MTD)" value={data.bot_replies_mtd} />
        </div>
        {data.last_message_at && (
          <p className="mt-3 text-xs text-muted-foreground/60">
            Last message: {formatMyt(data.last_message_at)}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
