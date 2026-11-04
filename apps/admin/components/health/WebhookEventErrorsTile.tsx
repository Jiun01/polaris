/**
 * Webhook & event errors tile (logging visibility — Phase A).
 *
 * Surfaces the ingest problems recorded in webhook_events over the last 24 h —
 * HMAC failures, tenant-not-found, ChannelNotEntitled, suspended-tenant drops —
 * broken out by status and error_class (the volume/freshness Webhook tiles don't
 * show these). Benign high-volume outcomes (replays, human-handling, demo) are
 * excluded server-side. Source: webhook_events via the system-health probe.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatInt, formatMyt } from "@/lib/format";
import type {
  WebhookEventBreakdown,
  WebhookEventCount,
  WebhookEventError,
} from "@/lib/types/health";

interface Props {
  data: WebhookEventBreakdown | null;
}

function CountRow({ c }: { c: WebhookEventCount }) {
  return (
    <div className="flex items-baseline justify-between gap-2 text-xs">
      <span className="truncate font-mono" title={c.key}>
        {c.key}
      </span>
      <span className="shrink-0 tabular-nums font-medium">
        {formatInt(c.count)}
      </span>
    </div>
  );
}

function RecentRow({ e }: { e: WebhookEventError }) {
  return (
    <div className="flex items-baseline justify-between gap-2 text-xs">
      <span className="truncate" title={`${e.source} · ${e.status}`}>
        <span className="text-muted-foreground/70">{e.source}</span>{" "}
        <span className="font-mono">{e.error_class ?? e.status}</span>
      </span>
      <span
        className="shrink-0 font-mono text-muted-foreground/60"
        suppressHydrationWarning
      >
        {formatMyt(e.received_at)}
      </span>
    </div>
  );
}

export function WebhookEventErrorsTile({ data }: Props) {
  if (!data || data.error) {
    // `!data` = whole snapshot missing; `data.error` = the probe itself failed
    // (DB outage). Both render a distinct unavailable state — NOT the green
    // "no errors" zero-state, which would be a misleading false-negative.
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            Webhook &amp; Event Errors
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground">Unavailable</p>
        </CardContent>
      </Card>
    );
  }

  const hasErrors = data.total_errors_24h > 0;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold text-muted-foreground">
          Webhook &amp; Event Errors
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="mb-3 flex flex-col gap-0.5">
          <span
            className={`text-xl font-bold tabular-nums ${hasErrors ? "text-destructive" : ""}`}
          >
            {formatInt(data.total_errors_24h)}
          </span>
          <span className="text-xs text-muted-foreground">
            Non-success events (24 h)
          </span>
        </div>

        {!hasErrors ? (
          <p className="text-xs text-muted-foreground">
            No webhook ingest errors in the last 24 h.
          </p>
        ) : (
          <div className="space-y-3">
            {data.by_status.length > 0 ? (
              <div className="space-y-1 border-t border-border pt-2">
                <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground/70">
                  By status
                </p>
                {data.by_status.map((c) => (
                  <CountRow key={`s-${c.key}`} c={c} />
                ))}
              </div>
            ) : null}

            {data.by_error_class.length > 0 ? (
              <div className="space-y-1 border-t border-border pt-2">
                <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground/70">
                  Top error types
                </p>
                {data.by_error_class.map((c) => (
                  <CountRow key={`e-${c.key}`} c={c} />
                ))}
              </div>
            ) : null}

            {data.recent.length > 0 ? (
              <div className="space-y-1 border-t border-border pt-2">
                <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground/70">
                  Most recent
                </p>
                {data.recent.map((e) => (
                  <RecentRow
                    key={`${e.received_at}-${e.source}-${e.status}`}
                    e={e}
                  />
                ))}
              </div>
            ) : null}
          </div>
        )}

        {data.grafana_url ? (
          <div className="mt-3 flex gap-3 border-t border-border pt-2 text-xs">
            <a
              href={data.grafana_url}
              target="_blank"
              rel="noreferrer"
              className="text-primary underline-offset-2 hover:underline"
            >
              Grafana ↗
            </a>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
