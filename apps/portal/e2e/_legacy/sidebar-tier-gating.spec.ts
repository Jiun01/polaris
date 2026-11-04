/**
 * Sidebar tier-gating E2E (Phase 8 PR-12).
 *
 * Starter tenants see lock icons on Broadcasts + Branches; Pro+ tenants
 * see them as active nav links. Driven by the seeded test fixtures
 * (one Starter, one Pro) — relies on the `pytest-` fixture pattern
 * documented in `../docs/internal-ops/RUNBOOK.md §portal e2e fixtures`.
 *
 * Skipped when PLAYWRIGHT_STORAGE_STATE_* env vars aren't set
 * (CI provides them via the test workflow's fixture step).
 */

import { expect, test } from "@playwright/test";

const STARTER_STATE = process.env.PLAYWRIGHT_STORAGE_STATE_STARTER;
const PRO_STATE = process.env.PLAYWRIGHT_STORAGE_STATE_PRO;

test.describe("Starter tier gates Broadcasts + Branches", () => {
  test.skip(() => !STARTER_STATE, "PLAYWRIGHT_STORAGE_STATE_STARTER not set");
  test.use({ storageState: STARTER_STATE ?? "" });

  test("sidebar shows lock icons + locked pages render upgrade card", async ({
    page,
  }) => {
    await page.goto("/dashboard");
    const sidebar = page.getByRole("navigation", { name: /primary/i });
    await expect(sidebar.getByText("Broadcasts")).toBeVisible();
    await expect(sidebar.getByText("Branches")).toBeVisible();
    // Lock icons live inside the locked nav items.
    await expect(
      sidebar.getByRole("img", { name: /upgrade required/i }),
    ).toHaveCount(2);

    await page.getByRole("link", { name: /broadcasts/i }).click();
    await expect(page).toHaveURL(/\/broadcasts(\/locked)?/);
    await expect(
      page.getByRole("heading", { name: /pro feature/i }),
    ).toBeVisible();
  });
});

test.describe("Pro tier unlocks Broadcasts + Branches", () => {
  test.skip(() => !PRO_STATE, "PLAYWRIGHT_STORAGE_STATE_PRO not set");
  test.use({ storageState: PRO_STATE ?? "" });

  test("Broadcasts route loads the real list", async ({ page }) => {
    await page.goto("/broadcasts");
    await expect(
      page.getByRole("heading", { name: /^broadcasts$/i }),
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: /new broadcast/i }),
    ).toBeVisible();
  });

  test("Branches route renders CRUD client", async ({ page }) => {
    await page.goto("/branches");
    await expect(
      page.getByRole("heading", { name: /^branches$/i }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: /add branch/i }),
    ).toBeVisible();
  });
});
