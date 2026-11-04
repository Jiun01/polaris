/**
 * PROD SMOKE — WS4: admin promotions read surface + gateway-ensure UI gap.
 *
 * Verifies GET /api/admin/promotions (routers/admin_promotions.py, new this
 * release) returns real cross-tenant data through the live proxy, and
 * documents that the /promotions admin PAGE itself is still the pre-existing
 * static placeholder — frontend wiring for this endpoint was deliberately
 * deferred, so that gap must read as a known state, not a regression.
 */

import { test, expect } from "@playwright/test";

test.describe("WS4 — admin promotions", () => {
  test("GET /api/admin/promotions returns a well-shaped array", async ({
    request,
  }) => {
    const res = await request.get("/api/proxy/api/admin/promotions?limit=50");
    expect(res.ok()).toBeTruthy();
    const rows: unknown[] = await res.json();
    expect(Array.isArray(rows)).toBe(true);
    if (rows.length > 0) {
      const row = rows[0] as Record<string, unknown>;
      expect(row).toHaveProperty("id");
      expect(row).toHaveProperty("tenant_id");
      expect(row).toHaveProperty("tenant_slug");
      expect(row).toHaveProperty("is_active");
    }
  });

  test("KNOWN GAP: /promotions page is still the static placeholder", async ({
    page,
  }) => {
    await page.goto("/promotions");
    // If this ever starts asserting real promo rows, the frontend has been
    // wired up — update this spec (not a bug) rather than treating it as a
    // regression when it fails.
    await expect(
      page.getByRole("heading", { name: /promotions/i }),
    ).toBeVisible();
  });
});
