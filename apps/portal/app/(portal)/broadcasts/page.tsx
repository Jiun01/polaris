/**
 * Broadcasts — Screen #7 (Phase 8 PR-9).
 *
 * Pro+ tier-gated per ADR-026 Am.3. Starter tenants see the locked
 * variant at /broadcasts/locked; the middleware here checks the
 * tenant_provider context and routes accordingly.
 *
 * Server Component. Lists past + scheduled broadcasts via
 * `GET /api/tenant/broadcasts/`.
 */

import Link from "next/link";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { TierLockRedirect } from "@/components/shell/TierLockRedirect";
import { formatAbsolute } from "@/lib/format";
import { cn } from "@/lib/utils";

type BroadcastStatus =
  | "draft"
  | "scheduled"
  | "sending"
  | "completed"
  | "failed"
  | "paused"
  | "cancelled";

interface BroadcastDTO {
  id: string;
  name: string;
  status: BroadcastStatus;
  scheduled_at: string | null;
  started_at: string | null;
  created_at: string;
  total_recipients: number;
  sent_count: number;
  delivered_count: number;
  read_count: number;
  failed_count: number;
}

const STATUS_TONE: Record<BroadcastStatus, string> = {
  draft: "bg-muted text-muted-foreground",
  scheduled: "bg-blue-100 text-blue-900 dark:bg-blue-950 dark:text-blue-100",
  sending: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-100",
  completed:
    "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100",
  failed: "bg-destructive/15 text-destructive",
  paused: "bg-muted text-muted-foreground",
  cancelled: "bg-muted text-muted-foreground",
};

async function loadBroadcasts(): Promise<{
  items: BroadcastDTO[];
  error?: string;
}> {
  try {
    const res = await apiFetchServer<BroadcastDTO[]>(TenantRoutes.broadcasts());
    return { items: res ?? [] };
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error("[portal /broadcasts] fetch failed", err);
    return {
      items: [],
      error:
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't load broadcasts.",
    };
  }
}

export default async function BroadcastsPage() {
  const { items, error } = await loadBroadcasts();

  return (
    <TierLockRedirect minTier="pro" lockedPath="/broadcasts/locked">
      <div className="space-y-6">
        <header className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-3xl font-semibold tracking-tight">
              Broadcasts
            </h1>
            <p className="mt-1 text-sm text-muted-foreground">
              Send approved WhatsApp templates to opted-in audience segments.
            </p>
          </div>
          <Button asChild>
            <Link href="/broadcasts/new">New broadcast</Link>
          </Button>
        </header>

        {error ? (
          <div
            role="status"
            className="rounded-lg border border-warning bg-warning/10 px-4 py-3 text-sm text-warning"
          >
            {error}
          </div>
        ) : null}

        {items.length === 0 && !error ? (
          <div className="rounded-xl border border-dashed bg-muted/30 p-8 text-center">
            <h2 className="text-base font-semibold">No broadcasts yet</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Compose your first broadcast: pick a template, filter your
              audience, and schedule.
            </p>
            <div className="mt-4">
              <Button asChild>
                <Link href="/broadcasts/new">Start a broadcast</Link>
              </Button>
            </div>
          </div>
        ) : (
          <div className="overflow-hidden rounded-xl border bg-card">
            <table className="w-full text-sm">
              <caption className="sr-only">Broadcasts by recency</caption>
              <thead className="border-b bg-muted/40 text-left text-xs uppercase text-muted-foreground">
                <tr>
                  <th scope="col" className="px-4 py-2.5 font-medium">
                    Broadcast
                  </th>
                  <th scope="col" className="px-4 py-2.5 font-medium">
                    Status
                  </th>
                  <th scope="col" className="px-4 py-2.5 font-medium">
                    Sent / Delivered / Read
                  </th>
                  <th scope="col" className="px-4 py-2.5 font-medium">
                    Schedule
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((b) => (
                  <tr
                    key={b.id}
                    className="border-b last:border-b-0 hover:bg-accent/30"
                  >
                    <td className="px-4 py-3 font-medium">{b.name}</td>
                    <td className="px-4 py-3">
                      <Badge
                        variant="outline"
                        className={cn(
                          "border-0 font-normal capitalize",
                          STATUS_TONE[b.status],
                        )}
                      >
                        {b.status}
                      </Badge>
                    </td>
                    <td className="px-4 py-3 tabular-nums text-muted-foreground">
                      {b.sent_count} / {b.delivered_count} / {b.read_count}
                      {b.failed_count > 0 ? (
                        <span className="ml-2 text-destructive">
                          ({b.failed_count} failed)
                        </span>
                      ) : null}
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">
                      {formatAbsolute(
                        b.scheduled_at ?? b.started_at ?? b.created_at,
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </TierLockRedirect>
  );
}
