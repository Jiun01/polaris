/**
 * NotificationsPage POM — /notifications (rebuilt suite).
 *
 * Selectors grounded in:
 *   - apps/portal/app/(portal)/notifications/page.tsx
 *   - apps/portal/components/notifications/MarkAllReadButton.tsx
 */

import type { Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class NotificationsPage extends BasePage {
  constructor(page: Page) {
    super(page);
  }

  async goto() {
    await super.goto("/notifications");
  }

  get heading() {
    return this.page.getByRole("heading", { name: /^notifications$/i });
  }

  /** Unread count badge — a Badge element containing "{n} unread" */
  get unreadBadge() {
    return this.page.getByText(/\d+ unread/i);
  }

  get markAllReadButton() {
    return this.page.getByRole("button", { name: /mark all read/i });
  }

  get fetchErrorBanner() {
    return this.page.getByRole("status");
  }

  get emptyHeading() {
    return this.page.getByRole("heading", { name: /no notifications yet/i });
  }

  /** Notification list — scoped to main content to avoid matching the sidebar nav list */
  get notificationList() {
    return this.page.getByRole("main").getByRole("list");
  }

  get notificationItems() {
    return this.page.getByRole("listitem");
  }

  /** The notification bell button in the top bar */
  get topBarBell() {
    return this.topbar.getByRole("button", { name: /notifications/i });
  }
}
