"use client";

/**
 * Notification poll loop (Phase 8 PR-11).
 *
 * Per Q-PHASE8-08: 30-second `setInterval` on
 * `/api/tenant/notifications/?since=<last_seen_at>` while the bell
 * is mounted; pause when `document.visibilityState === 'hidden'`;
 * resume on visibility change.
 *
 * The hook returns the most recent unread count + the latest 10
 * notifications (capped by `page_size=10`) for the bell dropdown.
 * Full-page filter view at /notifications fetches separately.
 */

import * as React from "react";

import { apiFetch } from "@/lib/api/client";
import { TenantRoutes } from "@/lib/api/routes";

const POLL_INTERVAL_MS = 30_000;

export interface NotificationItem {
  id: string;
  tenant_id: string;
  recipient_user_id: string | null;
  kind: string;
  title: string;
  body: string | null;
  href: string | null;
  metadata: Record<string, unknown>;
  read_at: string | null;
  created_at: string;
}

interface NotificationsResponse {
  items: NotificationItem[];
  unread_count: number;
  next_cursor: string | null;
}

export function useNotificationPoll() {
  const [unreadCount, setUnreadCount] = React.useState(0);
  const [items, setItems] = React.useState<NotificationItem[]>([]);
  const [error, setError] = React.useState<string | null>(null);

  const fetchOnce = React.useCallback(async () => {
    try {
      const res = await apiFetch<NotificationsResponse>(
        TenantRoutes.notifications({ page_size: 10 }),
      );
      if (!res) return;
      setItems(res.items);
      setUnreadCount(res.unread_count);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Poll failed");
    }
  }, []);

  React.useEffect(() => {
    let intervalId: number | null = null;

    function start() {
      if (intervalId !== null) return;
      void fetchOnce();
      intervalId = window.setInterval(() => {
        if (document.visibilityState !== "hidden") void fetchOnce();
      }, POLL_INTERVAL_MS);
    }

    function stop() {
      if (intervalId !== null) {
        window.clearInterval(intervalId);
        intervalId = null;
      }
    }

    function onVisibility() {
      if (document.visibilityState === "hidden") {
        stop();
      } else {
        start();
      }
    }

    start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [fetchOnce]);

  const markRead = React.useCallback(async (id: string) => {
    setItems((cur) =>
      cur.map((n) =>
        n.id === id ? { ...n, read_at: new Date().toISOString() } : n,
      ),
    );
    setUnreadCount((c) => Math.max(0, c - 1));
    try {
      await apiFetch(TenantRoutes.notification(id), {
        method: "PATCH",
        body: JSON.stringify({ read: true }),
      });
    } catch {
      // Optimistic state stays — next poll will reconcile.
    }
  }, []);

  const markAllRead = React.useCallback(async () => {
    const previousUnread = unreadCount;
    setItems((cur) =>
      cur.map((n) => ({
        ...n,
        read_at: n.read_at ?? new Date().toISOString(),
      })),
    );
    setUnreadCount(0);
    try {
      await apiFetch(TenantRoutes.notificationsMarkAllRead(), {
        method: "POST",
      });
    } catch {
      setUnreadCount(previousUnread);
    }
  }, [unreadCount]);

  return {
    unreadCount,
    items,
    error,
    markRead,
    markAllRead,
    refresh: fetchOnce,
  };
}
