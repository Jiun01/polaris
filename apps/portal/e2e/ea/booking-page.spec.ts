/**
 * EA public booking page (Easy!Appointments fork) — customer-facing booking UI.
 *
 * Ported from the retired admin/_legacy/phase-04-smoke.spec.ts into the ACTIVE
 * suite (the audit flagged the /book/{slug} wizard as the #1 behavioural blind
 * spot — it only existed in _legacy and never ran). Covers the API-route auth surface
 * "EA public booking page": branded /book/{slug}, no Easy!Appointments leak,
 * bad slug → 404, the booking wizard renders.
 *
 * No portal auth needed — this is the public EA fork at :8090. Uses absolute
 * URLs so it is independent of the portal baseURL.
 *
 * Seed prereq: `uv run python apps/api/scripts/seed_e2e_ea_providers.py --reset`
 * (EA booking slugs = provider username = email-prefix from that seed).
 */

import { expect, test } from "@playwright/test";

const EA = process.env.EA_BASE_URL ?? "http://localhost:8090";

// EA booking slugs minted by seed_e2e_ea_providers.py — ONE per tenant.
// ea_polaris_tenants PK is tenant_id, and /book/{slug} → from_slug() resolves
// slug -> tenant (tenant-level booking; the wizard picks the provider/branch).
// The multi-branch tenant-d therefore has a single public slug (its HQ);
// per-branch routing is exercised by the conversational flow in
// cross-path/branch-ea-routing.spec.ts, not a distinct /book slug.
const SLUGS = [
  "provider-polaris",
  "provider-tenant-c",
  "provider-tenant-d-hq",
] as const;

const FORBIDDEN = ["easy!appointments", "easyappointments", "chatwoot"];

function assertNoLeak(text: string, ctx: string): void {
  const lc = text.toLowerCase();
  for (const f of FORBIDDEN) {
    expect(lc.includes(f), `${ctx}: whitelabel leak "${f}"`).toBe(false);
  }
}

test.describe("EA public booking page (/book/{slug})", () => {
  for (const slug of SLUGS) {
    test(`${slug}: reachable, Polaris-branded, no Easy!Appointments leak`, async ({
      page,
      request,
    }) => {
      const res = await request.get(`${EA}/book/${slug}`);
      expect(res.status(), `GET /book/${slug}`).toBe(200);

      await page.goto(`${EA}/book/${slug}`, { waitUntil: "domcontentloaded" });

      // If the EA seed wasn't run, the slug 404s — surface that as a skip, not a
      // false pass (the 404 page has its own title).
      const title = await page.title();
      test.skip(
        /404|not found/i.test(title),
        `EA provider slug '${slug}' not seeded — run seed_e2e_ea_providers.py --reset`,
      );

      // Title is Polaris-branded (e.g. "... | Polaris Calendar").
      expect(title.toLowerCase()).toContain("polaris");
      assertNoLeak(title, `title slug=${slug}`);

      const body = await page.locator("body").innerText();
      assertNoLeak(body, `body slug=${slug}`);
    });

    test(`${slug}: booking wizard renders (service step or auto-advance)`, async ({
      page,
    }) => {
      await page.goto(`${EA}/book/${slug}`, { waitUntil: "domcontentloaded" });
      const title = await page.title();
      test.skip(
        /404|not found/i.test(title),
        `EA provider slug '${slug}' not seeded`,
      );

      // The wizard container must be present.
      await expect(page.locator("#book-appointment-wizard")).toBeVisible({
        timeout: 10_000,
      });

      // Single service+provider auto-advances to step 2 (date picker), hiding
      // #select-service; multi-service shows the service dropdown. Accept either.
      const serviceSelect = page.locator("#select-service");
      if (await serviceSelect.isVisible().catch(() => false)) {
        expect(
          await serviceSelect.locator("option").count(),
          "service dropdown must have >=1 option",
        ).toBeGreaterThan(0);
      } else {
        await expect(page.locator("#wizard-frame-2")).toBeVisible({
          timeout: 10_000,
        });
      }
    });
  }

  test("unknown slug → branded 404 page (no leak)", async ({ page }) => {
    await page.goto(`${EA}/book/zzz-not-a-real-slug-9999`, {
      waitUntil: "domcontentloaded",
    });
    const title = await page.title();
    expect(title.toLowerCase()).toMatch(/404|not found/);
    // 404 page is still Polaris-branded and leak-free.
    assertNoLeak(title, "404 title");
    assertNoLeak(await page.locator("body").innerText(), "404 body");
    // NOTE: the EA fork serves the 404 *content* with HTTP 200 (soft-404) — a
    // hard 404 status would be preferable. Asserting content, not status, here.
  });
});
