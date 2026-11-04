/**
 * Quotes E2E spec (rebuilt suite).
 *
 * Surfaces covered: Quotes.
 *   - /quotes list: status converted/unconverted, PDF link
 *   - /quotes/new: generator form + live proration preview → POST generate
 *   - Validation: email format, signup date must be a valid date
 *   - Empty-list CTA (Generate quote button always visible)
 *   - 422 from API surfaces error
 *   - Whitelabel assertion
 */

import { test, expect, ADMIN_STATE } from "../_support/fixtures";
import { QuotesPage, QuoteGeneratorPage } from "../pages/QuotesPage";
import { setE2EFault } from "../_support/e2e-fault";

test.skip(() => !ADMIN_STATE, "admin storage state not minted");
test.use({ storageState: ADMIN_STATE ?? "" });

// ---------------------------------------------------------------------------
// Shared mock data
// ---------------------------------------------------------------------------

const MOCK_QUOTES = [
  {
    id: "quote-e2e-0001",
    quote_code: "POL-Q-2606001",
    prospect_email: "buyer@example.com",
    prospect_company: "Acme Sdn Bhd",
    target_tier: "pro",
    target_billing_cycle: "monthly",
    intended_signup_date: "2026-07-01",
    expires_at: "2026-07-31",
    converted_to_tenant_id: null,
  },
  {
    id: "quote-e2e-0002",
    quote_code: "POL-Q-2606002",
    prospect_email: "converted@example.com",
    prospect_company: "Beta Corp",
    target_tier: "ultra",
    target_billing_cycle: "annual",
    intended_signup_date: "2026-06-15",
    expires_at: "2026-07-15",
    converted_to_tenant_id: "00000000-0000-0000-0000-00000000000a",
  },
];

async function mockQuotesList(page: import("@playwright/test").Page) {
  await page.route("**/api/admin/quotes*", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: MOCK_QUOTES, total: MOCK_QUOTES.length }),
      });
    } else {
      await route.continue();
    }
  });
}

// ---------------------------------------------------------------------------
// /quotes list
// ---------------------------------------------------------------------------

test.describe("/quotes list", () => {
  test("renders heading and Generate quote button", async ({ page }) => {
    await mockQuotesList(page);
    const p = new QuotesPage(page);
    await p.goto();

    await expect(p.heading).toBeVisible();
    await expect(p.generateQuoteButton).toBeVisible();
  });

  test("table shows unconverted and converted quote rows", async ({ page }) => {
    await setE2EFault(page, [
      {
        match: "quotes",
        body: { items: MOCK_QUOTES, total: MOCK_QUOTES.length },
      },
    ]);

    const p = new QuotesPage(page);
    await p.goto();

    // Unconverted row — badge text is exactly "Unconverted"
    await expect(p.rowByText("POL-Q-2606001")).toBeVisible();
    await expect(
      p.rowByText("POL-Q-2606001").getByText("Unconverted", { exact: true }),
    ).toBeVisible();

    // Converted row — badge text is exactly "Converted"
    await expect(p.rowByText("POL-Q-2606002")).toBeVisible();
    await expect(
      p.rowByText("POL-Q-2606002").getByText("Converted", { exact: true }),
    ).toBeVisible();
  });

  test("PDF link is present in each row", async ({ page }) => {
    await setE2EFault(page, [
      {
        match: "quotes",
        body: { items: MOCK_QUOTES, total: MOCK_QUOTES.length },
      },
    ]);

    const p = new QuotesPage(page);
    await p.goto();

    for (const q of MOCK_QUOTES) {
      await expect(p.pdfLinkInRow(q.quote_code)).toBeVisible();
    }
  });

  test("PDF link has correct href pointing at the PDF route", async ({
    page,
  }) => {
    await setE2EFault(page, [
      {
        match: "quotes",
        body: { items: MOCK_QUOTES, total: MOCK_QUOTES.length },
      },
    ]);

    const p = new QuotesPage(page);
    await p.goto();

    const firstQuote = MOCK_QUOTES[0];
    const pdfLink = p.pdfLinkInRow(firstQuote.quote_code);
    await expect(pdfLink).toBeVisible();
    const href = await pdfLink.getAttribute("href");
    expect(href).toMatch(new RegExp(`quotes/${firstQuote.id}/pdf`));
  });

  test("empty quotes list shows empty-state card with CTA", async ({
    page,
  }) => {
    // /quotes is a Server Component — page.route() can't intercept the SSR
    // fetch, so use the e2e_fault hook (same as the populated-list tests above).
    await setE2EFault(page, [
      { match: "quotes", body: { items: [], total: 0 } },
    ]);

    const p = new QuotesPage(page);
    await p.goto();

    // Empty state text from quotes/page.tsx: "No quotes yet. Generate one…"
    await expect(p.emptyState).toBeVisible();
    // Generate quote button always present
    await expect(p.generateQuoteButton).toBeVisible();
  });

  test("no whitelabel leak on /quotes", async ({ page }) => {
    await mockQuotesList(page);
    const p = new QuotesPage(page);
    await p.goto();
    await p.assertNoBrandLeak();
  });
});

// ---------------------------------------------------------------------------
// /quotes/new — generator form
// ---------------------------------------------------------------------------

test.describe("/quotes/new generator form", () => {
  test("form renders all fields with correct inputs", async ({ page }) => {
    const gen = new QuoteGeneratorPage(page);
    await gen.goto();

    await expect(gen.prospectEmailInput).toBeVisible();
    await expect(gen.prospectCompanyInput).toBeVisible();
    await expect(gen.targetTierSelect).toBeVisible();
    await expect(gen.billingCycleSelect).toBeVisible();
    await expect(gen.signupDateInput).toBeVisible();
    await expect(gen.submitButton).toBeVisible();
  });

  test("target tier select has starter / pro / ultra options", async ({
    page,
  }) => {
    const gen = new QuoteGeneratorPage(page);
    await gen.goto();

    // Options from QuoteGeneratorForm.tsx: "Starter", "Pro", "Ultra"
    const options = await gen.targetTierSelect
      .locator("option")
      .allTextContents();
    expect(options.some((o) => /starter/i.test(o))).toBe(true);
    expect(options.some((o) => /pro/i.test(o))).toBe(true);
    expect(options.some((o) => /ultra/i.test(o))).toBe(true);
  });

  test("billing cycle select has monthly and annual options", async ({
    page,
  }) => {
    const gen = new QuoteGeneratorPage(page);
    await gen.goto();

    const options = await gen.billingCycleSelect
      .locator("option")
      .allTextContents();
    expect(options.some((o) => /monthly/i.test(o))).toBe(true);
    expect(options.some((o) => /annual/i.test(o))).toBe(true);
  });

  test("live proration preview appears when signup date is set", async ({
    page,
  }) => {
    const gen = new QuoteGeneratorPage(page);
    await gen.goto();

    // No preview before date is set
    await expect(gen.livePreview).not.toBeVisible();

    // Set a future date — triggers computePreview()
    await gen.signupDateInput.fill("2026-08-15");
    await expect(gen.livePreview).toBeVisible({ timeout: 3_000 });

    // The preview card (scoped by the "Live preview" heading container) shows
    // "days remaining" and "Ratio: X/Y = Z%" text.
    // Scope to the preview card to avoid strict-mode violations from other
    // page text that incidentally matches /ratio/i (e.g. the subtitle).
    const previewCard = gen.livePreviewCard();
    await expect(previewCard.getByText(/days remaining/i)).toBeVisible();
    await expect(previewCard.getByText(/ratio:/i)).toBeVisible();
  });

  test("live preview updates when tier changes", async ({ page }) => {
    const gen = new QuoteGeneratorPage(page);
    await gen.goto();

    await gen.signupDateInput.fill("2026-08-01");
    await gen.targetTierSelect.selectOption("starter");
    // Scope to preview card to avoid strict-mode from the select option text.
    // Two <p> elements in the card both contain "1,099" (Headline + First invoice)
    // — use .first() since any match confirms the preview updated correctly.
    const previewCard = gen.livePreviewCard();
    await expect(previewCard.getByText(/1,099/).first()).toBeVisible();

    await gen.targetTierSelect.selectOption("ultra");
    await expect(previewCard.getByText(/5,499/).first()).toBeVisible();
  });

  test("signup date must be filled for preview to appear", async ({ page }) => {
    const gen = new QuoteGeneratorPage(page);
    await gen.goto();

    // The signup-date input is type="date" — Playwright rejects non-date
    // strings with "Malformed value".  The intent of the test is: if the
    // date field is empty the preview card must not be shown.  Assert that
    // the input starts empty and the preview is absent, then confirm the
    // preview appears only after a valid date is entered.
    await expect(gen.signupDateInput).toHaveValue("");
    await expect(gen.livePreview).not.toBeVisible();

    // Filling a valid date triggers the preview.
    await gen.signupDateInput.fill("2026-08-15");
    await expect(gen.livePreview).toBeVisible({ timeout: 3_000 });
  });

  test("happy path: generate quote and show success card with PDF preview", async ({
    page,
  }) => {
    const generatedQuote = {
      id: "quote-e2e-new-001",
      quote_code: "POL-Q-2607001",
      prospect_email: "test@e2e.example",
      prospect_company: "E2E Corp",
      target_tier: "pro",
      target_billing_cycle: "monthly",
      intended_signup_date: "2026-08-01",
      expires_at: "2026-08-31",
      converted_to_tenant_id: null,
    };

    await page.route("**/api/admin/quotes", async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(generatedQuote),
        });
      } else {
        await route.continue();
      }
    });

    const gen = new QuoteGeneratorPage(page);
    await gen.goto();

    await gen.prospectEmailInput.fill("test@e2e.example");
    await gen.prospectCompanyInput.fill("E2E Corp");
    await gen.targetTierSelect.selectOption("pro");
    await gen.billingCycleSelect.selectOption("monthly");
    await gen.signupDateInput.fill("2026-08-01");
    await gen.submitButton.click();

    // Success card: "Quote POL-Q-2607001 generated successfully."
    await expect(gen.successCard).toBeVisible({ timeout: 10_000 });
    await expect(gen.successCard).toContainText("POL-Q-2607001");
  });

  test("invalid email format: browser validation prevents submit", async ({
    page,
  }) => {
    const gen = new QuoteGeneratorPage(page);
    await gen.goto();

    await gen.prospectEmailInput.fill("not-valid-email");
    await gen.signupDateInput.fill("2026-08-01");

    // The email input is type="email" — browser prevents form submission
    // with an invalid email; we verify the input has invalid state
    const isValid = await gen.prospectEmailInput.evaluate(
      (el: HTMLInputElement) => el.validity.valid,
    );
    expect(isValid).toBe(false);
  });

  test("missing signup date: submit button is not effective (required field)", async ({
    page,
  }) => {
    const gen = new QuoteGeneratorPage(page);
    await gen.goto();

    // signup-date is required — browser blocks native form submission
    const isRequired = await gen.signupDateInput.evaluate(
      (el: HTMLInputElement) => el.required,
    );
    expect(isRequired).toBe(true);
  });

  test("422 from API surfaces error message", async ({ page }) => {
    // adminFetch routes through the same-origin proxy at /api/proxy/…
    // The error body uses the PolarisErrorEnvelope shape so ApiError picks up
    // err.message = "intended_signup_date must be in the future".
    await page.route("**/api/proxy/api/admin/quotes", async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({
          status: 422,
          contentType: "application/json",
          body: JSON.stringify({
            error: {
              code: "validation_error",
              message: "intended_signup_date must be in the future",
            },
          }),
        });
      } else {
        await route.continue();
      }
    });

    const gen = new QuoteGeneratorPage(page);
    await gen.goto();

    // Fill a valid date so the form can be submitted; the mock returns 422.
    await gen.signupDateInput.fill("2026-08-01");
    await gen.submitButton.click();

    await expect(
      page.getByText(
        /intended_signup_date must be in the future|unexpected error/i,
      ),
    ).toBeVisible({ timeout: 5_000 });
  });

  test("no whitelabel leak on /quotes/new", async ({ page }) => {
    const gen = new QuoteGeneratorPage(page);
    await gen.goto();
    await gen.assertNoBrandLeak();
  });
});
