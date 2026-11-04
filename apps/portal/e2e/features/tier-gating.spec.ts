/**
 * Tier-gating matrix E2E — rebuilt suite.
 *
 * Surfaces covered: Cross-cutting tier-gating matrix.
 *   - Locked routes → /broadcasts/locked upgrade card for starter
 *   - Pro+ routes render real content, not locked state
 *   - Sidebar lock icons present on starter for gated items
 *   - Representative routes × {starter=TENANTS.starterA, pro=TENANTS.proC, ultra=TENANTS.ultra}
 *
 * Does NOT re-test all 14 surfaces in depth — that's each surface's own spec.
 * This file covers the *matrix* of tier × route in one place.
 *
 * INFRA NOTE: NextAuth /api/auth/* returns 500 on the current running stack,
 * so all session cookies are rejected and every portal route redirects to
 * /login. All tests are fixme'd until the auth infra is restored.
 * A previously "passing" test (#60: /broadcasts/locked redirects pro to
 * /broadcasts list) was a false positive — it tested `not.toHaveURL(/locked/)`
 * which trivially passed because the page was on /login, not /broadcasts/locked.
 */

import { test, expect, TENANTS, requireState } from "../_support/fixtures";

const starterA = TENANTS.starterA;
const proC = TENANTS.proC;
const ultra = TENANTS.ultra;

// ---------------------------------------------------------------------------
// Starter tier — locked routes
// ---------------------------------------------------------------------------

test.describe("Tier-gating starter — /broadcasts locked [starter]", () => {
  test.skip(() => !starterA.state, "starter-a storage state not minted");
  test.use({ storageState: requireState(starterA) });

  test("/broadcasts redirects to /broadcasts/locked", async ({ page }) => {
    await page.goto("/broadcasts");
    await page.waitForURL(/\/broadcasts\/locked/);
  });

  test("/broadcasts/locked renders upgrade card heading", async ({ page }) => {
    await page.goto("/broadcasts/locked");
    await expect(
      page.getByRole("heading", { name: /broadcasts are a pro feature/i }),
    ).toBeVisible();
  });

  test("/broadcasts/locked upgrade CTA points to plan-change?target=pro", async ({
    page,
  }) => {
    await page.goto("/broadcasts/locked");
    const link = page.getByRole("link", { name: /upgrade to pro/i });
    await expect(link).toBeVisible();
    const href = await link.getAttribute("href");
    expect(href).toMatch(/\/billing\/plan-change\?target=pro/);
  });
});

// ---------------------------------------------------------------------------
// Starter tier — sidebar lock icons
// ---------------------------------------------------------------------------

test.describe("Tier-gating starter — sidebar lock icons [starter]", () => {
  test.skip(() => !starterA.state, "starter-a storage state not minted");
  test.use({ storageState: requireState(starterA) });

  test("sidebar shows Broadcasts nav item", async ({ page }) => {
    await page.goto("/dashboard");
    const sidebar = page.getByRole("navigation", { name: /primary/i });
    await expect(sidebar.getByText("Broadcasts")).toBeVisible();
  });

  test("sidebar shows Branches nav item with lock indicator", async ({
    page,
  }) => {
    await page.goto("/dashboard");
    const sidebar = page.getByRole("navigation", { name: /primary/i });
    await expect(sidebar.getByText("Branches")).toBeVisible();
    // Lock icons — as per legacy sidebar-tier-gating.spec.ts pattern
    await expect(
      sidebar.getByRole("img", { name: /upgrade required/i }),
    ).toHaveCount(2);
  });
});

// ---------------------------------------------------------------------------
// Starter tier — accessible routes still work
// ---------------------------------------------------------------------------

test.describe("Tier-gating starter — accessible routes [starter]", () => {
  test.skip(() => !starterA.state, "starter-a storage state not minted");
  test.use({ storageState: requireState(starterA) });

  test("/dashboard renders for starter", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(
      page.getByRole("heading", { name: /^dashboard$/i }),
    ).toBeVisible();
  });

  test("/promotions renders for starter (not tier-locked)", async ({
    page,
  }) => {
    await page.goto("/promotions");
    await expect(
      page.getByRole("heading", { name: /^promotions$/i }),
    ).toBeVisible();
  });

  test("/channels renders for starter", async ({ page }) => {
    await page.goto("/channels");
    await expect(
      page.getByRole("heading", { name: /^channels$/i }),
    ).toBeVisible();
  });

  test("/settings renders for starter", async ({ page }) => {
    await page.goto("/settings");
    await expect(
      page.getByRole("heading", { name: /^settings$/i }),
    ).toBeVisible();
  });

  test("/billing renders for starter", async ({ page }) => {
    await page.goto("/billing");
    await expect(
      page.getByRole("heading", { name: /billing & usage/i }),
    ).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Pro tier — locked routes are unlocked
// ---------------------------------------------------------------------------

test.describe("Tier-gating pro — /broadcasts unlocked [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("/broadcasts renders list page for pro (not locked)", async ({
    page,
  }) => {
    await page.goto("/broadcasts");
    // Should NOT redirect to /locked
    await expect(page).not.toHaveURL(/\/broadcasts\/locked/);
    await expect(
      page.getByRole("heading", { name: /^broadcasts$/i }),
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: /new broadcast/i }),
    ).toBeVisible();
  });

  test("/broadcasts/locked redirects pro to the live /broadcasts list", async ({
    page,
  }) => {
    // broadcasts/locked/page.tsx wraps its upgrade card in TierUnlockRedirect
    // (the inverse of TierLockRedirect): a pro+ tenant landing on the locked
    // page is bounced to the real /broadcasts list, so they never see the
    // upgrade card. Starter tenants (gated out of /broadcasts) still see it.
    await page.goto("/broadcasts/locked");
    await page.waitForURL((url) => url.pathname === "/broadcasts", {
      timeout: 10_000,
    });
  });
});

// ---------------------------------------------------------------------------
// Ultra tier — full access
// ---------------------------------------------------------------------------

test.describe("Tier-gating ultra — full access [ultra]", () => {
  test.skip(() => !ultra.state, "ultra storage state not minted");
  test.use({ storageState: requireState(ultra) });

  test("/broadcasts renders list page for ultra", async ({ page }) => {
    await page.goto("/broadcasts");
    await expect(page).not.toHaveURL(/\/locked/);
    await expect(
      page.getByRole("heading", { name: /^broadcasts$/i }),
    ).toBeVisible();
  });

  test("/dashboard renders for ultra", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(
      page.getByRole("heading", { name: /^dashboard$/i }),
    ).toBeVisible();
  });

  test("/billing renders for ultra", async ({ page }) => {
    await page.goto("/billing");
    await expect(
      page.getByRole("heading", { name: /billing & usage/i }),
    ).toBeVisible();
  });

  test("/settings renders for ultra", async ({ page }) => {
    await page.goto("/settings");
    await expect(
      page.getByRole("heading", { name: /^settings$/i }),
    ).toBeVisible();
  });
});
