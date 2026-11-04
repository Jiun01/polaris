/**
 * Recent failures tile (System Health enrichment 2026-06-01).
 *
 * Last-hour failure counters (errored broadcasts + failed recipients) plus
 * deep-links out to the full APM tooling (Grafana / Sentry) where the traces
 * and logs live. Links render only when their URLs are configured.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatInt } from "@/lib/format";
import type { RecentFailures } from "@/lib/types/health";

interface Props {
  data: RecentFailures | null;
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span
        className={`text-xl font-bold tabular-nums ${value > 0 ? "text-destructive" : ""}`}
      >
        {formatInt(value)}
      </span>
      <span className="text-xs text-muted-foreground">{label}</span>
    </div>
  );
}

export function RecentFailuresTile({ data }: Props) {
  if (!data) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            Recent Failures
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground">Unavailable</p>
        </CardContent>
      </Card>
    );
  }

  const hasLinks = data.grafana_url || data.sentry_url;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold text-muted-foreground">
          Recent Failures
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-4">
          <Metric
            label="Errored broadcasts (1 h)"
            value={data.errored_broadcasts_last_hour}
          />
          <Metric
            label="Failed recipients (1 h)"
            value={data.failed_recipients_last_hour}
          />
        </div>
        <div className="mt-3 flex gap-3 border-t border-border pt-2 text-xs">
          {data.grafana_url ? (
            <a
              href={data.grafana_url}
              target="_blank"
              rel="noreferrer"
              className="text-primary underline-offset-2 hover:underline"
            >
              Grafana ↗
            </a>
          ) : null}
          {data.sentry_url ? (
            <a
              href={data.sentry_url}
              target="_blank"
              rel="noreferrer"
              className="text-primary underline-offset-2 hover:underline"
            >
              Sentry ↗
            </a>
          ) : null}
          {!hasLinks ? (
            <span className="text-muted-foreground/60">
              APM links not configured
            </span>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}
