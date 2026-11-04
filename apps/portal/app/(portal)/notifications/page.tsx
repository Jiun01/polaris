/**
 * Notifications full-page view (Phase 8 PR-11).
 *
 * Server Component fetches the first page from
 * `/api/tenant/notifications/?page_size=50` and renders everything
 * — read + unread together so the operator can scan recent history.
 * The bell dropdown handles the unread-focused quick view.
 */

import Link from "next/link";
import { Bell } from "lucide-react";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { Badge } from "@/components/ui/badge";
import { MarkAllReadButton } from "@/components/notifications/MarkAllReadButton";
import { formatAbsolute, formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";

interface NotificationDTO {
  id: string;
  kind: string;
  title: string;
  body: string | null;
  href: string | null;
  read_at: string | null;
  created_at: string;
}

interface NotificationsResponse {
  items: NotificationDTO[];
  unread_count: number;
  next_cursor: string | null;
}

async function loadNotifications(): Promise<{
  items: NotificationDTO[];
  unread: number;
  error?: string;
}> {
  try {
    const res = await apiFetchServer<NotificationsResponse>(
      TenantRoutes.notifications({ page_size: 50 }),
    );
    return {
      items: res?.items ?? [],
      unread: res?.unread_count ?? 0,
    };
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error("[portal /notifications] fetch failed", err);
    return {
      items: [],
      unread: 0,
      error:
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't load notifications.",
    };
  }
}

export default async function NotificationsPage() {
  const { items, unread, error } = await loadNotifications();

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <Bell className="h-5 w-5 text-muted-foreground" aria-hidden />
            <h1 className="text-3xl font-semibold tracking-tight">
              Notifications
            </h1>
            {unread > 0 ? (
              <Badge
                variant="outline"
                className="border-0 bg-destructive text-xs text-destructive-foreground"
              >
                {unread} unread
              </Badge>
            ) : null}
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            Template approvals, broadcast results, billing, quota alerts, and
            ingestion status.
          </p>
        </div>
        {unread > 0 ? <MarkAllReadButton /> : null}
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
          <h2 className="text-base font-semibold">No notifications yet</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Important events (template approvals, broadcast completions, quota
            alerts) will show up here as they happen.
          </p>
        </div>
      ) : (
        <ul className="space-y-2">
          {items.map((n) => (
            <li
              key={n.id}
              className={cn(
                "rounded-xl border bg-card p-4",
                !n.read_at && "border-foreground/30",
              )}
            >
              {n.href ? (
                <Link href={n.href} className="block hover:underline">
                  <NotificationBody item={n} />
                </Link>
              ) : (
                <NotificationBody item={n} />
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function NotificationBody({ item }: { item: NotificationDTO }) {
  return (
    <div>
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold">{item.title}</h3>
          <p className="text-[11px] uppercase tracking-wide text-muted-foreground">
            {item.kind.replace(/_/g, " ")}
          </p>
        </div>
        {!item.read_at ? (
          <span
            aria-label="Unread"
            className="mt-1 h-2 w-2 shrink-0 rounded-full bg-foreground"
          />
        ) : null}
      </div>
      {item.body ? (
        <p className="mt-2 text-sm text-muted-foreground">{item.body}</p>
      ) : null}
      <p className="mt-2 text-xs text-muted-foreground">
        {formatRelative(item.created_at)} · {formatAbsolute(item.created_at)}
      </p>
    </div>
  );
}
