/**
 * APM / Sentry errors tile (System Health enrichment 2026-06-09).
 *
 * Shows Sentry error count for the last 24 h when configured, with a
 * deep-link to the Sentry project. Renders a muted hint when Sentry
 * is not configured.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ApmErrors } from "@/lib/types/health";

interface Props {
  data: ApmErrors | null;
}

export function ApmErrorsTile({ data }: Props) {
  if (!data) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            APM / Sentry Errors
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground">Unavailable</p>
        </CardContent>
      </Card>
    );
  }

  if (!data.configured) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-semibold text-muted-foreground">
            APM / Sentry Errors
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="mt-3 flex gap-3 border-t border-border pt-2 text-xs">
            <span className="text-muted-foreground/60">
              Sentry not configured
            </span>
          </div>
        </CardContent>
      </Card>
    );
  }

  // error_count_24h is `X | None = Field(None, ...)` in Pydantic — nullable
  // AND has a default, so json-schema-to-typescript marks the property
  // optional (`?:`) on top of the `number | null` union. It's always
  // present on the wire; treat undefined same as null.
  const errorCount = data.error_count_24h ?? null;
  const hasErrors = errorCount !== null && errorCount > 0;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold text-muted-foreground">
          APM / Sentry Errors
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-1">
        <p
          className={`text-2xl font-bold tabular-nums${hasErrors ? " text-destructive" : ""}`}
        >
          {errorCount !== null ? errorCount : "—"}
        </p>
        <p className="text-xs text-muted-foreground">Errors (24 h)</p>
        {data.error && (
          <p className="mt-2 text-xs text-muted-foreground/60">{data.error}</p>
        )}
        {data.sentry_url && (
          <div className="mt-3 flex gap-3 border-t border-border pt-2 text-xs">
            <a
              href={data.sentry_url}
              target="_blank"
              rel="noreferrer"
              className="text-primary underline-offset-2 hover:underline"
            >
              Open Sentry ↗
            </a>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
