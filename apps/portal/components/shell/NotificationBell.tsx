"use client";

/**
 * Bell icon in the topbar (Phase 8 PR-11).
 *
 * Click → dropdown of latest 10 notifications + "See all" link.
 * Unread count badge on the bell.
 */

import * as React from "react";
import Link from "next/link";
import { Bell } from "lucide-react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { useNotificationPoll } from "@/lib/notifications/poll";
import { formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";

export function NotificationBell() {
  const { unreadCount, items, markRead, markAllRead } = useNotificationPoll();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="relative h-9 w-9 p-0"
          aria-label={`Notifications${unreadCount > 0 ? ` (${unreadCount} unread)` : ""}`}
        >
          <Bell className="h-4 w-4" aria-hidden />
          {unreadCount > 0 ? (
            <span
              aria-hidden
              className="absolute right-1.5 top-1.5 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full bg-destructive px-1 text-[10px] font-semibold leading-none text-destructive-foreground"
            >
              {unreadCount > 99 ? "99+" : unreadCount}
            </span>
          ) : null}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80 p-0">
        <div className="flex items-center justify-between border-b px-3 py-2">
          <h2 className="text-sm font-semibold">Notifications</h2>
          {unreadCount > 0 ? (
            <button
              type="button"
              className="text-xs text-muted-foreground hover:text-foreground"
              onClick={() => void markAllRead()}
            >
              Mark all read
            </button>
          ) : null}
        </div>

        {items.length === 0 ? (
          <div className="px-3 py-8 text-center text-sm text-muted-foreground">
            You&apos;re all caught up.
          </div>
        ) : (
          <ul className="max-h-[400px] overflow-y-auto">
            {items.map((n) => (
              <li key={n.id}>
                <Link
                  href={n.href ?? "/notifications"}
                  onClick={() => void markRead(n.id)}
                  className={cn(
                    "block border-b border-border/50 px-3 py-2 transition-colors hover:bg-accent/30",
                    !n.read_at && "bg-accent/20",
                  )}
                >
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-sm font-medium">{n.title}</span>
                    {!n.read_at ? (
                      <span
                        aria-hidden
                        className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-foreground"
                      />
                    ) : null}
                  </div>
                  {n.body ? (
                    <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
                      {n.body}
                    </p>
                  ) : null}
                  <p className="mt-1 text-[11px] text-muted-foreground">
                    {formatRelative(n.created_at)}
                  </p>
                </Link>
              </li>
            ))}
          </ul>
        )}

        <div className="border-t bg-muted/20 px-3 py-2 text-center">
          <Link
            href="/notifications"
            className="text-xs font-medium text-foreground hover:underline"
          >
            See all notifications
          </Link>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
