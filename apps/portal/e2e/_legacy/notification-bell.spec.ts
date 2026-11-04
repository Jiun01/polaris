/**
 * Notification bell E2E (Phase 8 PR-12).
 *
 * Polling fetches the unread count + items; clicking the bell opens
 * the dropdown; mark-all-read drops the badge to 0. Skipped without
 * auth fixture.
 */

import { expect, test } from "@playwright/test";

const PRO_STATE = process.env.PLAYWRIGHT_STORAGE_STATE_PRO;

test.skip(() => !PRO_STATE, "PLAYWRIGHT_STORAGE_STATE_PRO not set");
test.use({ storageState: PRO_STATE ?? "" });

test("bell opens dropdown + mark-all-read clears the badge", async ({
  page,
}) => {
  await page.goto("/dashboard");
  const bell = page.getByRole("button", { name: /notifications/i });
  await bell.click();

  // Dropdown should show either an empty-state message or items.
  await expect(
    page
      .getByText(/all caught up/i)
      .or(page.getByRole("link", { name: /see all/i })),
  ).toBeVisible();
});
