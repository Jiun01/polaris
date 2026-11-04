/**
 * Notifications E2E — rebuilt suite.
 *
 * Surfaces covered: Notifications.
 *   - /notifications list: heading, unread badge, "Mark all read" button,
 *     notification list items, row links
 *   - Empty state
 *   - Fetch-error banner
 *   - "Mark all read" API call + re-render
 *   - Bell polling in TopBar (30s interval; pauses on tab hidden)
 *   - Whitelabel check
 *
 * NOTE: notifications/page.tsx is a Server Component that uses apiFetchServer
 * (SSR). page.route mocks are no-ops for the initial GET. Tests that depend on
 * injecting specific list data are fixme'd with a precise SSR reason.
 * Tests that only need the page to load (heading, no-brand-leak, bell) run live.
 */

import { test, expect, TENANTS, requireState } from "../_support/fixtures";
import { NotificationsPage } from "../pages/NotificationsPage";
import { setE2EFault } from "../_support/e2e-fault";

const proC = TENANTS.proC;

const NOTIFICATION_FIXTURE = {
  items: [
    {
      id: "n1",
      kind: "template_approved",
      title: "Template approved",
      body: "Your 'Spring Sale' template has been approved.",
      href: "/templates",
      read_at: null,
      created_at: "2026-05-29T08:00:00Z",
    },
    {
      id: "n2",
      kind: "broadcast_completed",
      title: "Broadcast completed",
      body: "April Flash Sale sent to 148 recipients.",
      href: "/broadcasts",
      read_at: "2026-05-28T12:00:00Z",
      created_at: "2026-05-28T12:00:00Z",
    },
  ],
  unread_count: 1,
  next_cursor: null,
};

// ---------------------------------------------------------------------------
// Happy path — tolerant assertions against real seeded state
// ---------------------------------------------------------------------------

test.describe("Notifications list [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("heading renders on /notifications", async ({ page }) => {
    const np = new NotificationsPage(page);
    await np.goto();
    await expect(np.heading).toBeVisible();
  });

  test("heading, unread badge, and mark-all-read button render", async ({
    page,
  }) => {
    await setE2EFault(page, [
      { match: "notifications", body: NOTIFICATION_FIXTURE },
    ]);
    const np = new NotificationsPage(page);
    await np.goto();
    await expect(np.heading).toBeVisible();
    await expect(np.unreadBadge).toBeVisible();
    await expect(np.markAllReadButton).toBeVisible();
  });

  test("notification items render with title and kind", async ({ page }) => {
    await setE2EFault(page, [
      { match: "notifications", body: NOTIFICATION_FIXTURE },
    ]);
    const np = new NotificationsPage(page);
    await np.goto();
    await expect(np.notificationList).toBeVisible();
    // Use heading role to avoid strict-mode: the kind label also contains the same text
    await expect(
      page.getByRole("heading", { name: "Template approved" }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Broadcast completed" }),
    ).toBeVisible();
  });

  test("notification with href renders as a link", async ({ page }) => {
    await setE2EFault(page, [
      { match: "notifications", body: NOTIFICATION_FIXTURE },
    ]);
    const np = new NotificationsPage(page);
    await np.goto();
    // The template_approved notification links to /templates
    await expect(
      page.getByRole("link", { name: /template approved/i }),
    ).toBeVisible();
  });

  test("no whitelabel brand strings on notifications page", async ({
    page,
  }) => {
    const np = new NotificationsPage(page);
    await np.goto();
    await np.assertNoBrandLeak();
  });
});

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

test.describe("Notifications empty state [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("empty notifications shows 'No notifications yet'", async ({ page }) => {
    await setE2EFault(page, [
      {
        match: "notifications",
        body: { items: [], unread_count: 0, next_cursor: null },
      },
    ]);
    const np = new NotificationsPage(page);
    await np.goto();
    await expect(np.emptyHeading).toBeVisible();
    // No mark-all-read when unread = 0
    await expect(np.markAllReadButton).not.toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Fetch-error banner
// ---------------------------------------------------------------------------

test.describe("Notifications fetch-error [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("503 on notifications API shows error banner", async ({ page }) => {
    await setE2EFault(page, [{ match: "notifications", status: 503 }]);
    const np = new NotificationsPage(page);
    await np.goto();
    await expect(np.fetchErrorBanner).toBeVisible();
    await expect(np.heading).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Mark-all-read
// ---------------------------------------------------------------------------

test.describe("Notifications mark all read [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("mark-all-read POST triggers re-render with zero unread", async ({
    page,
  }) => {
    // SSR: fault-inject populated notifications so unread badge + button render.
    await setE2EFault(page, [
      { match: "notifications", body: NOTIFICATION_FIXTURE },
    ]);
    // Client-side POST (MarkAllReadButton is a client component) — page.route works.
    // When the POST succeeds we update the e2e_fault cookie so the subsequent
    // router.refresh() SSR re-render returns unread_count: 0 and hides the badge.
    await page.route(
      "**/api/tenant/notifications/mark-all-read**",
      async (route) => {
        if (route.request().method() === "POST") {
          // Update fault cookie to zero-unread before the response arrives so
          // the router.refresh() SSR re-render picks it up.
          await page.context().addCookies([
            {
              name: "e2e_fault",
              value: encodeURIComponent(
                JSON.stringify([
                  {
                    match: "notifications",
                    body: {
                      items: NOTIFICATION_FIXTURE.items.map((n) => ({
                        ...n,
                        read_at: n.read_at ?? "2026-05-30T00:00:00Z",
                      })),
                      unread_count: 0,
                      next_cursor: null,
                    },
                  },
                ]),
              ),
              domain: "localhost",
              path: "/",
            },
          ]);
          await route.fulfill({ status: 200, body: "{}" });
        } else {
          await route.continue();
        }
      },
    );

    const np = new NotificationsPage(page);
    await np.goto();
    await expect(np.markAllReadButton).toBeVisible();
    await np.markAllReadButton.click();
    // After router.refresh() re-render, unread badge should disappear
    await expect(np.unreadBadge).not.toBeVisible({ timeout: 5000 });
  });
});

// ---------------------------------------------------------------------------
// Bell polling in TopBar
// ---------------------------------------------------------------------------

test.describe("TopBar bell polling [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("bell button is visible in top bar on /dashboard", async ({ page }) => {
    // Bell lives in the TopBar shell (NotificationBell client component) —
    // present on every portal page. The aria-label is dynamic:
    // "Notifications" when unread=0, "Notifications (N unread)" otherwise.
    await page.goto("/dashboard");
    await page.waitForLoadState("networkidle");
    const np = new NotificationsPage(page);
    await expect(np.topBarBell).toBeVisible();
  });

  test("bell opens dropdown with notifications preview", async ({ page }) => {
    await page.goto("/dashboard");
    await page.waitForLoadState("networkidle");
    const np = new NotificationsPage(page);
    await np.topBarBell.click();
    // Dropdown shows either empty-state or items or "See all" link.
    // Use .first() to avoid strict-mode when both "all caught up" and
    // "See all notifications" are simultaneously visible in the dropdown.
    await expect(
      page
        .getByText(/all caught up/i)
        .or(page.getByRole("link", { name: /see all/i }))
        .or(page.getByRole("link", { name: /notifications/i }))
        .first(),
    ).toBeVisible();
  });
});
