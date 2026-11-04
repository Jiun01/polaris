/**
 * Templates E2E — rebuilt suite.
 *
 * Surfaces covered: Templates.
 *   - /templates list: heading, "New template" CTA, status badges, rejection reason
 *   - /templates list: empty state
 *   - /templates list: fetch-error banner
 *   - /templates/new: no-WA → connect CTA
 *   - /templates/new form: Step 1 — name / language / category
 *   - /templates/new form: Step 2 — body with variable detection
 *   - /templates/new form: Step 3 — preview + submit
 *   - /templates/new: WhatsApp channel picker (>1 channel)
 *   - Submit → POST /api/tenant/templates/
 *   - No whitelabel leak
 */

import { test, expect, TENANTS, requireState } from "../_support/fixtures";
import { TemplatesPage } from "../pages/TemplatesPage";
import { setE2EFault } from "../_support/e2e-fault";

const proC = TENANTS.proC;
const starterB = TENANTS.starterB; // Instagram-only; no WhatsApp

const FAKE_TEMPLATE_ID = "00000000-0000-0000-0000-dddddddd0001";

const MOCK_WA_CHANNEL = {
  id: "00000000-0000-0000-0000-cccccccc1001",
  channel: "whatsapp",
  status: "active",
};

function mockTemplateApis(
  page: import("@playwright/test").Page,
  templates: object[] = [],
) {
  page.route("**/api/tenant/templates/**", async (route) => {
    if (route.request().method() === "POST") {
      return route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          id: FAKE_TEMPLATE_ID,
          name: "test_template",
          language: "en",
          category: "UTILITY",
          status: "pending",
          rejection_reason: null,
          last_status_change_at: null,
          submitted_at: new Date().toISOString(),
          created_at: new Date().toISOString(),
        }),
      });
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(templates),
    });
  });
}

function mockChannelsApi(
  page: import("@playwright/test").Page,
  whatsappChannels: object[] = [MOCK_WA_CHANNEL],
) {
  page.route("**/api/tenant/channels", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: whatsappChannels,
        per_kind: {
          whatsapp: whatsappChannels.filter(
            (c: any) => c.channel === "whatsapp",
          ).length,
          instagram: 0,
          messenger: 0,
        },
      }),
    });
  });
}

// ---------------------------------------------------------------------------
// List page — happy path
// ---------------------------------------------------------------------------

test.describe("Templates list [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("heading and New template button render", async ({ page }) => {
    // SSR — mocks no-op. Heading and New template button always render.
    const tp = new TemplatesPage(page);
    await tp.goto();
    await expect(tp.heading).toBeVisible();
    await expect(tp.newTemplateButton).toBeVisible();
  });

  test("templates list shows all status badge types", async ({ page }) => {
    await setE2EFault(page, [
      {
        match: "templates/",
        body: [
          {
            id: "t1",
            name: "welcome_msg",
            language: "en",
            category: "UTILITY",
            status: "approved",
            rejection_reason: null,
            last_status_change_at: new Date().toISOString(),
            submitted_at: new Date().toISOString(),
            created_at: new Date().toISOString(),
          },
          {
            id: "t2",
            name: "promo_blast",
            language: "en",
            category: "MARKETING",
            status: "rejected",
            rejection_reason: "Variable format not allowed",
            last_status_change_at: new Date().toISOString(),
            submitted_at: new Date().toISOString(),
            created_at: new Date().toISOString(),
          },
          {
            id: "t3",
            name: "otp_login",
            language: "ms_MY",
            category: "AUTHENTICATION",
            status: "pending",
            rejection_reason: null,
            last_status_change_at: null,
            submitted_at: new Date().toISOString(),
            created_at: new Date().toISOString(),
          },
          {
            id: "t4",
            name: "sale_alert",
            language: "en",
            category: "MARKETING",
            status: "paused",
            rejection_reason: null,
            last_status_change_at: new Date().toISOString(),
            submitted_at: new Date().toISOString(),
            created_at: new Date().toISOString(),
          },
        ],
      },
    ]);
    const tp = new TemplatesPage(page);
    await tp.goto();
    await expect(tp.statusBadge("approved")).toBeVisible();
    await expect(tp.statusBadge("rejected")).toBeVisible();
    await expect(tp.statusBadge("pending")).toBeVisible();
    await expect(tp.statusBadge("paused")).toBeVisible();
  });

  test("rejection reason is shown inline for rejected templates", async ({
    page,
  }) => {
    await setE2EFault(page, [
      {
        match: "templates/",
        body: [
          {
            id: "t2",
            name: "promo_blast",
            language: "en",
            category: "MARKETING",
            status: "rejected",
            rejection_reason: "Variable format not allowed",
            last_status_change_at: new Date().toISOString(),
            submitted_at: new Date().toISOString(),
            created_at: new Date().toISOString(),
          },
        ],
      },
    ]);
    const tp = new TemplatesPage(page);
    await tp.goto();
    await expect(
      tp.rejectionReason(/variable format not allowed/i),
    ).toBeVisible();
  });

  test("no whitelabel leak on list page", async ({ page }) => {
    // SSR — mocks no-op. assertNoBrandLeak checks real rendered content.
    const tp = new TemplatesPage(page);
    await tp.goto();
    await tp.assertNoBrandLeak();
  });
});

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

test.describe("Templates empty state [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("empty state shows submit-first-template CTA", async ({ page }) => {
    await setE2EFault(page, [{ match: "templates/", body: [] }]);
    const tp = new TemplatesPage(page);
    await tp.goto();
    await expect(tp.emptyState).toBeVisible();
    await expect(
      page.getByRole("link", { name: /submit your first template/i }),
    ).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Fetch-error banner
// ---------------------------------------------------------------------------

test.describe("Templates fetch error [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("503 on templates API shows error banner", async ({ page }) => {
    await setE2EFault(page, [{ match: "templates/", status: 503 }]);
    const tp = new TemplatesPage(page);
    await tp.goto();
    await expect(tp.fetchErrorBanner).toBeVisible();
    await expect(tp.fetchErrorBanner).toContainText(
      /couldn't load your templates/i,
    );
  });
});

// ---------------------------------------------------------------------------
// /templates/new — no WhatsApp channel
// ---------------------------------------------------------------------------

test.describe("Templates new — no WhatsApp [starterB]", () => {
  test.skip(() => !starterB.state, "starter-b storage state not minted");
  test.use({ storageState: requireState(starterB) });

  test("no-WA state shows connect-channels CTA", async ({ page }) => {
    await setE2EFault(page, [
      {
        match: "channels",
        body: {
          items: [{ id: "ig-ch-1", channel: "instagram", status: "active" }],
          per_kind: { whatsapp: 0, instagram: 1, messenger: 0 },
        },
      },
    ]);
    const tp = new TemplatesPage(page);
    await tp.gotoNew();
    await expect(tp.noWhatsAppCta).toBeVisible();
    await expect(
      page.getByRole("link", { name: /go to channels/i }),
    ).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// /templates/new — full form happy path [pro]
// ---------------------------------------------------------------------------

test.describe("Templates new form [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("Step 1 fields render: name / language / category / channel picker hidden when single WA channel", async ({
    page,
  }) => {
    // SSR — mockChannelsApi is a no-op; real proC WA channels are used.
    // proC has 1 WA channel so channel picker should be hidden (real behavior).
    const tp = new TemplatesPage(page);
    await tp.gotoNew();
    await expect(tp.nameInput).toBeVisible();
    await expect(tp.languageInput).toBeVisible();
    await expect(tp.categoryCard("Utility")).toBeVisible();
    await expect(tp.categoryCard("Marketing")).toBeVisible();
    await expect(tp.categoryCard("Authentication")).toBeVisible();
    // Channel picker hidden when ≤1 WA channel (real seeded proC has exactly 1)
    await expect(tp.channelPicker).not.toBeVisible();
  });

  test("channel picker is shown when tenant has >1 WhatsApp channel", async ({
    page,
  }) => {
    await setE2EFault(page, [
      {
        match: "channels",
        body: {
          items: [MOCK_WA_CHANNEL, { ...MOCK_WA_CHANNEL, id: "wa-ch-2" }],
          per_kind: { whatsapp: 2, instagram: 0, messenger: 0 },
        },
      },
    ]);
    const tp = new TemplatesPage(page);
    await tp.gotoNew();
    await expect(tp.channelPicker).toBeVisible();
  });

  test("Step 1 → Step 2: fill name + language + click Next: Compose", async ({
    page,
  }) => {
    // SSR — mockChannelsApi is a no-op; real proC WA channels are used.
    const tp = new TemplatesPage(page);
    await tp.gotoNew();
    await tp.nameInput.fill("order_confirmation");
    await tp.languageInput.fill("en");
    await tp.categoryCard("Utility").click();
    await tp.nextComposeButton.click();
    await expect(tp.bodyTextarea).toBeVisible();
  });

  test("Step 2: variable detection — typing {{1}} shows example fill input", async ({
    page,
  }) => {
    // SSR — mockChannelsApi is a no-op; real proC WA channels are used.
    const tp = new TemplatesPage(page);
    await tp.gotoNew();
    await tp.nameInput.fill("order_update");
    await tp.languageInput.fill("en");
    await tp.nextComposeButton.click();
    await tp.bodyTextarea.fill("Hi {{1}}, your order {{2}} is ready.");
    await expect(page.getByLabel("{{1}}")).toBeVisible();
    await expect(page.getByLabel("{{2}}")).toBeVisible();
  });

  test("Step 2 → Step 3: body filled → Next: Preview enabled → click proceeds", async ({
    page,
  }) => {
    // SSR — mockChannelsApi is a no-op; real proC WA channels are used.
    const tp = new TemplatesPage(page);
    await tp.gotoNew();
    await tp.nameInput.fill("booking_confirm");
    await tp.languageInput.fill("ms_MY");
    await tp.nextComposeButton.click();
    await tp.bodyTextarea.fill("Your booking is confirmed.");
    await tp.nextPreviewButton.click();
    await expect(tp.submitButton).toBeVisible();
  });

  test("Step 3: preview renders body text", async ({ page }) => {
    // SSR — mockChannelsApi is a no-op; real proC WA channels are used.
    const tp = new TemplatesPage(page);
    await tp.gotoNew();
    await tp.nameInput.fill("faq_reply");
    await tp.languageInput.fill("en");
    await tp.nextComposeButton.click();
    await tp.bodyTextarea.fill("Here is the answer to your question.");
    await tp.nextPreviewButton.click();
    await expect(
      page.getByText(/here is the answer to your question/i),
    ).toBeVisible();
  });

  test("Step 3: Submit to Meta calls POST and redirects to /templates", async ({
    page,
  }) => {
    // SSR — mockChannelsApi/mockTemplateApis are no-ops for the initial page load.
    // The POST on submit IS a browser fetch — page.route intercepts it correctly.
    let postBody: unknown;
    await page.route("**/api/tenant/templates/**", async (route) => {
      if (route.request().method() === "POST") {
        postBody = await route.request().postDataJSON();
        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({
            id: FAKE_TEMPLATE_ID,
            name: "submit_test",
            language: "en",
            category: "UTILITY",
            status: "pending",
            rejection_reason: null,
            last_status_change_at: null,
            submitted_at: new Date().toISOString(),
            created_at: new Date().toISOString(),
          }),
        });
      } else {
        await route.continue();
      }
    });

    const tp = new TemplatesPage(page);
    await tp.gotoNew();
    await tp.nameInput.fill("submit_test");
    await tp.languageInput.fill("en");
    await tp.categoryCard("Utility").click();
    await tp.nextComposeButton.click();
    await tp.bodyTextarea.fill("Test body content.");
    await tp.nextPreviewButton.click();
    await tp.submitButton.click();
    // Should redirect to /templates
    await expect(page).toHaveURL(/\/templates$/);
    // POST was called with correct shape
    expect((postBody as any).name).toBe("submit_test");
    expect((postBody as any).category).toBe("UTILITY");
  });

  test("submit failure shows error alert", async ({ page }) => {
    // SSR — mockChannelsApi is a no-op; real proC WA channels are used.
    await page.route("**/api/tenant/templates/**", async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({
          status: 422,
          contentType: "application/json",
          body: JSON.stringify({ detail: "Template name already exists" }),
        });
      } else {
        await route.continue();
      }
    });

    const tp = new TemplatesPage(page);
    await tp.gotoNew();
    await tp.nameInput.fill("duplicate_name");
    await tp.languageInput.fill("en");
    await tp.nextComposeButton.click();
    await tp.bodyTextarea.fill("Some body text.");
    await tp.nextPreviewButton.click();
    await tp.submitButton.click();
    await expect(tp.errorAlert).toBeVisible();
  });

  test("no whitelabel leak on new template form", async ({ page }) => {
    // SSR — mockChannelsApi is a no-op; real proC WA channels used.
    const tp = new TemplatesPage(page);
    await tp.gotoNew();
    await tp.assertNoBrandLeak();
  });
});
