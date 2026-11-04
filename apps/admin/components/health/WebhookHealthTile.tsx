/**
 * Webhook health tile (System Health enrichment 2026-06-09).
 *
 * Reusable — rendered once for Telegram, once for Meta. Shows event
 * throughput (last 1 h) plus HMAC-fail and tenant-not-found error
 * counters, highlighted when non-zero.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatInt, formatMyt } from "@/lib/format";
import type { WebhookSurfaceHealth } from "@/lib/types/health";

interface Props {
  title: string;
  data: WebhookSurfaceHealth | null;
}

function errorCountClass(value: number): string {
  if (value > 0) return "text-destructive";
  return "";
}

export function WebhookHealthTile({ title, data }: Props) {
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
        <CardTitle className="text-sm font-semibold text-muted-foreground">
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-col gap-0.5">
          <span className="text-2xl font-bold tabular-nums">
            {formatInt(data.events_1h)}
          </span>
          <span className="text-xs text-muted-foreground">Events (1 h)</span>
        </div>
        <div className="grid grid-cols-2 gap-4">
          <div className="flex flex-col gap-0.5">
            <span
              className={`text-xl font-bold tabular-nums ${errorCountClass(data.hmac_fail_1h)}`}
            >
              {formatInt(data.hmac_fail_1h)}
            </span>
            <span className="text-xs text-muted-foreground">
              HMAC fail (1 h)
            </span>
          </div>
          <div className="flex flex-col gap-0.5">
            <span
              className={`text-xl font-bold tabular-nums ${errorCountClass(data.tenant_not_found_1h)}`}
            >
              {formatInt(data.tenant_not_found_1h)}
            </span>
            <span className="text-xs text-muted-foreground">
              Tenant not found (1 h)
            </span>
          </div>
        </div>
        {data.last_received_at ? (
          <p className="text-xs text-muted-foreground/60">
            Last received: {formatMyt(data.last_received_at)}
          </p>
        ) : (
          <p className="text-xs text-muted-foreground/60">
            No webhooks received
          </p>
        )}
      </CardContent>
    </Card>
  );
}
