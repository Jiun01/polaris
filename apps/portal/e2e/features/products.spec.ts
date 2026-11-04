/**
 * Products E2E — Phase 15 PR-8.
 *
 * Surfaces covered (mirrors the Promotions suite):
 *   - List: heading + "New product" button, cards-or-empty-state, fetch-error
 *   - Empty state → "Add your first product" CTA
 *   - /products/new form (name / sku / price / description / active toggle)
 *   - Branch dropdown [pro+] when branches seeded
 *   - /products/[id] edit form + photo gallery (Add photo) + delete w/ confirm
 *   - 404 on a non-existent product
 *   - Whitelabel check
 *
 * INFRA NOTE (same as promotions.spec.ts): the list/detail/branches reads are
 * SSR (apiFetchServer) so page.route() can't inject them — use setE2EFault.
 * Only ProductForm's client POST/PATCH/DELETE + multipart upload are
 * page.route-mockable once auth infra is restored.
 */

import { test, expect, TENANTS, requireState } from "../_support/fixtures";
import { ProductsPage } from "../pages/ProductsPage";
import { setE2EFault } from "../_support/e2e-fault";

const proC = TENANTS.proC;
const proD = TENANTS.proD; // has multi_branch_enabled

const PRODUCT_FIXTURE = {
  id: "prod-1",
  tenant_id: proC.id,
  tenant_branch_id: null,
  sku: "TUMBLER-BLUE",
  name: "Blue Tumbler",
  description: "500ml insulated steel tumbler in matte blue.",
  price_text: "RM 49.90",
  is_active: true,
  images: [],
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-01T00:00:00Z",
};

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

test.describe("Products list [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("heading and new-product button render", async ({ page }) => {
    const pp = new ProductsPage(page);
    await pp.goto();
    await expect(pp.heading).toBeVisible();
    await expect(pp.newProductButton).toBeVisible();
  });

  test("product cards or empty state render", async ({ page }) => {
    const pp = new ProductsPage(page);
    await pp.goto();
    const cards = page.locator("main a[href^='/products/']");
    const count = await cards.count();
    if (count > 0) {
      await expect(cards.first()).toBeVisible();
    } else {
      await expect(pp.emptyHeading).toBeVisible();
    }
  });

  test("no whitelabel brand strings on products list", async ({ page }) => {
    const pp = new ProductsPage(page);
    await pp.goto();
    await pp.assertNoBrandLeak();
  });
});

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

test.describe("Products empty state [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("empty list shows add-first-product CTA", async ({ page }) => {
    await setE2EFault(page, [{ match: "products/", body: [] }]);
    const pp = new ProductsPage(page);
    await pp.goto();
    await expect(pp.emptyHeading).toBeVisible();
    await expect(pp.createFirstProductLink).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Fetch-error banner
// ---------------------------------------------------------------------------

test.describe("Products fetch-error [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("503 shows error banner while heading stays visible", async ({
    page,
  }) => {
    await setE2EFault(page, [{ match: "products/", status: 503 }]);
    const pp = new ProductsPage(page);
    await pp.goto();
    await expect(pp.fetchErrorBanner).toBeVisible();
    await expect(pp.heading).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// /products/new form
// ---------------------------------------------------------------------------

test.describe("Products new form [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("form fields render with back link", async ({ page }) => {
    await setE2EFault(page, [{ match: "tenant/branches", body: [] }]);
    const pp = new ProductsPage(page);
    await pp.gotoNew();
    await expect(pp.newProductHeading).toBeVisible();
    await expect(pp.nameInput).toBeVisible();
    await expect(pp.skuInput).toBeVisible();
    await expect(pp.priceInput).toBeVisible();
    await expect(pp.descriptionTextarea).toBeVisible();
    await expect(pp.isActiveCheckbox).toBeVisible();
    await expect(pp.createButton).toBeVisible();
    await expect(
      page.getByRole("link", { name: /all products/i }),
    ).toBeVisible();
  });

  test("name required: empty name blocks submit", async ({ page }) => {
    await setE2EFault(page, [{ match: "tenant/branches", body: [] }]);
    const pp = new ProductsPage(page);
    await pp.gotoNew();
    await pp.descriptionTextarea.fill("A description without a name.");
    await pp.createButton.click();
    await expect(page).toHaveURL(/\/products\/new/);
  });

  test("branch dropdown visible when tenant has branches [pro-d]", async ({
    page,
  }) => {
    test.skip(!proD.state, "pro-d storage state not minted");
    await setE2EFault(page, [
      {
        match: "tenant/branches",
        body: [
          { id: "00000000-0000-0000-0000-0000000d0001", name: "HQ" },
          { id: "00000000-0000-0000-0000-0000000d0002", name: "Branch 2" },
        ],
      },
    ]);
    const pp = new ProductsPage(page);
    await pp.gotoNew();
    await expect(pp.branchSelect).toBeVisible();
    const options = await pp.branchSelect.locator("option").allTextContents();
    expect(options.length).toBeGreaterThan(1);
  });
});

// ---------------------------------------------------------------------------
// /products/[id] — edit + gallery + delete
// ---------------------------------------------------------------------------

test.describe("Products edit and delete [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("edit page renders pre-populated form with gallery + delete", async ({
    page,
  }) => {
    await setE2EFault(page, [
      { match: "products/prod-1", body: PRODUCT_FIXTURE },
      { match: "branches/", body: [] },
    ]);
    const pp = new ProductsPage(page);
    await pp.gotoEdit("prod-1");
    await expect(pp.editHeading).toBeVisible();
    await expect(pp.nameInput).toHaveValue("Blue Tumbler");
    await expect(pp.addPhotoLabel).toBeVisible();
    await expect(pp.deleteButton).toBeVisible();
  });

  test("delete button triggers confirmation dialog", async ({ page }) => {
    await setE2EFault(page, [
      { match: "products/prod-1", body: PRODUCT_FIXTURE },
      { match: "branches/", body: [] },
    ]);
    const pp = new ProductsPage(page);
    await pp.gotoEdit("prod-1");
    await expect(pp.deleteButton).toBeVisible();

    let dialogMessage = "";
    page.once("dialog", async (dialog) => {
      dialogMessage = dialog.message();
      await dialog.dismiss();
    });
    await pp.deleteButton.click();
    await page.waitForTimeout(200);
    expect(dialogMessage).toMatch(/Blue Tumbler/i);
  });
});

// ---------------------------------------------------------------------------
// 404
// ---------------------------------------------------------------------------

test.describe("Products 404 [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("non-existent product navigates to Next.js 404 page", async ({
    page,
  }) => {
    await setE2EFault(page, [
      { match: "products/no-such-id", status: 404 },
      { match: "branches/", body: [] },
    ]);
    const pp = new ProductsPage(page);
    await pp.gotoEdit("no-such-id");
    await expect(page).toHaveURL(/no-such-id/);
    await expect(
      page.getByRole("heading", { name: /404|not found/i }),
    ).toBeVisible();
  });
});
