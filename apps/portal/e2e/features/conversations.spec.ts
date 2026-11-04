/**
 * Conversations E2E — rebuilt suite.
 *
 * Surfaces covered: Conversations.
 *   - Channel filter chips render (All / WhatsApp / Instagram / Messenger / Demo)
 *   - Clicking a chip rewrites the URL query string
 *   - Cursor pagination — "Older →" link appears when next_cursor is set
 *   - Row → detail transcript (role / intent / model / latency / RAG chunks)
 *   - "Reply in inbox" external link present on detail page
 *   - 404 on missing conversation_id → Next.js notFound()
 *   - Empty state → import / connect CTA
 *   - Fetch-error banner
 *   - No whitelabel leak
 */

import { test, expect, TENANTS, requireState } from "../_support/fixtures";
import { ConversationsPage } from "../pages/ConversationsPage";
import { setE2EFault } from "../_support/e2e-fault";

const proC = TENANTS.proC;

// ---------------------------------------------------------------------------
// Helpers — seed API mocks (used only for client-side fetch tests)
// ---------------------------------------------------------------------------

const FAKE_CONVERSATION_ID = "00000000-0000-0000-0000-cccccccc0001";

function mockConversationList(
  page: import("@playwright/test").Page,
  overrides: object = {},
) {
  return page.route("**/api/tenant/conversations/**", async (route) => {
    // Only intercept the list call (no trailing UUID segment)
    const url = route.request().url();
    if (url.includes(FAKE_CONVERSATION_ID)) {
      return route.continue();
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [
          {
            id: FAKE_CONVERSATION_ID,
            tenant_id: proC.id,
            channel: "whatsapp",
            channel_user_id: "+601112345678",
            customer_display_name: "Test Customer",
            last_message_at: new Date().toISOString(),
            last_intent: "faq",
            message_count: 5,
            chatwoot_conversation_id: 42,
            created_at: new Date().toISOString(),
          },
        ],
        next_cursor: null,
        ...overrides,
      }),
    });
  });
}

function mockConversationDetail(page: import("@playwright/test").Page) {
  return page.route(
    `**/api/tenant/conversations/${FAKE_CONVERSATION_ID}`,
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: FAKE_CONVERSATION_ID,
          tenant_id: proC.id,
          channel: "whatsapp",
          channel_user_id: "+601112345678",
          customer_display_name: "Test Customer",
          chatwoot_conversation_id: 42,
          last_message_at: new Date().toISOString(),
          created_at: new Date().toISOString(),
          messages: [
            {
              id: "msg-001",
              role: "user",
              content: "Hello, I need help",
              intent: null,
              model_used: null,
              response_time_ms: null,
              input_tokens: null,
              output_tokens: null,
              cache_read_tokens: null,
              rag_chunk_count: 0,
              created_at: new Date().toISOString(),
            },
            {
              id: "msg-002",
              role: "assistant",
              content: "Hi! How can I assist you today?",
              intent: "greeting",
              model_used: "claude-haiku-3",
              response_time_ms: 412,
              input_tokens: 120,
              output_tokens: 22,
              cache_read_tokens: 0,
              rag_chunk_count: 2,
              created_at: new Date().toISOString(),
            },
          ],
        }),
      });
    },
  );
}

// ---------------------------------------------------------------------------
// List page tests
// ---------------------------------------------------------------------------

test.describe("Conversations list [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("page heading renders", async ({ page }) => {
    const conv = new ConversationsPage(page);
    await conv.goto();
    await expect(conv.heading).toBeVisible();
  });

  test("channel filter chips render for all channels", async ({ page }) => {
    const conv = new ConversationsPage(page);
    await conv.goto();
    for (const label of [
      "All",
      "WhatsApp",
      "Instagram",
      "Messenger",
      "Demo",
    ] as const) {
      await expect(conv.channelChip(label)).toBeVisible();
    }
  });

  test("clicking WhatsApp chip rewrites URL", async ({ page }) => {
    const conv = new ConversationsPage(page);
    await conv.goto();
    await conv.channelChip("WhatsApp").click();
    await expect(page).toHaveURL(/channel=whatsapp/);
  });

  test("clicking All chip clears channel filter", async ({ page }) => {
    const conv = new ConversationsPage(page);
    await page.goto("/conversations?channel=whatsapp");
    await page.waitForLoadState("networkidle");
    await conv.channelChip("All").click();
    await expect(page).toHaveURL(/\/conversations(?!.*channel=)/);
  });

  test('"Older →" link appears when next_cursor is present', async ({
    page,
  }) => {
    await setE2EFault(page, [
      {
        match: "tenant/conversations/",
        body: {
          items: [
            {
              id: FAKE_CONVERSATION_ID,
              tenant_id: proC.id,
              channel: "whatsapp",
              channel_user_id: "+601112345678",
              customer_display_name: "Test Customer",
              last_message_at: new Date().toISOString(),
              last_intent: "faq",
              message_count: 5,
              chatwoot_conversation_id: 42,
              created_at: new Date().toISOString(),
            },
          ],
          next_cursor: "cursor-abc123",
        },
      },
    ]);
    const conv = new ConversationsPage(page);
    await conv.goto();
    await expect(conv.olderLink).toBeVisible();
    const href = await conv.olderLink.getAttribute("href");
    expect(href).toMatch(/cursor=cursor-abc123/);
  });

  test("no whitelabel leak", async ({ page }) => {
    const conv = new ConversationsPage(page);
    await conv.goto();
    await conv.assertNoBrandLeak();
  });
});

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

test.describe("Conversations empty state [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("empty state shows connect-channel CTA", async ({ page }) => {
    await setE2EFault(page, [
      {
        match: "tenant/conversations/",
        body: { items: [], next_cursor: null },
      },
    ]);
    const conv = new ConversationsPage(page);
    await conv.goto();
    await expect(conv.emptyState).toBeVisible();
    await expect(
      page.getByRole("link", { name: /check your channel connections/i }),
    ).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Fetch-error banner
// ---------------------------------------------------------------------------

test.describe("Conversations fetch error [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("503 from API shows error banner", async ({ page }) => {
    await setE2EFault(page, [{ match: "tenant/conversations/", status: 503 }]);
    const conv = new ConversationsPage(page);
    await conv.goto();
    await expect(conv.fetchErrorBanner).toBeVisible();
    await expect(conv.fetchErrorBanner).toContainText(
      /having trouble loading your conversations/i,
    );
  });
});

// ---------------------------------------------------------------------------
// Detail page — transcript
// ---------------------------------------------------------------------------

test.describe("Conversation detail [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  /** Shared fault body for all detail-page tests. */
  async function injectDetailFault(page: import("@playwright/test").Page) {
    await setE2EFault(page, [
      {
        match: FAKE_CONVERSATION_ID,
        body: {
          id: FAKE_CONVERSATION_ID,
          tenant_id: proC.id,
          channel: "whatsapp",
          channel_user_id: "+601112345678",
          customer_display_name: "Test Customer",
          chatwoot_conversation_id: 42,
          last_message_at: new Date().toISOString(),
          created_at: new Date().toISOString(),
          messages: [
            {
              id: "msg-001",
              role: "user",
              content: "Hello, I need help",
              intent: null,
              model_used: null,
              response_time_ms: null,
              input_tokens: null,
              output_tokens: null,
              cache_read_tokens: null,
              rag_chunk_count: 0,
              created_at: new Date().toISOString(),
            },
            {
              id: "msg-002",
              role: "assistant",
              content: "Hi! How can I assist you today?",
              intent: "greeting",
              model_used: "claude-haiku-3",
              response_time_ms: 412,
              input_tokens: 120,
              output_tokens: 22,
              cache_read_tokens: 0,
              rag_chunk_count: 2,
              created_at: new Date().toISOString(),
            },
          ],
        },
      },
    ]);
  }

  test("transcript renders user and bot messages", async ({ page }) => {
    await injectDetailFault(page);
    const conv = new ConversationsPage(page);
    await conv.gotoDetail(FAKE_CONVERSATION_ID);

    await expect(conv.transcriptList).toBeVisible();
    await expect(page.getByText("Hello, I need help")).toBeVisible();
    await expect(
      page.getByText("Hi! How can I assist you today?"),
    ).toBeVisible();
  });

  test("transcript shows intent badge on bot message", async ({ page }) => {
    await injectDetailFault(page);
    const conv = new ConversationsPage(page);
    await conv.gotoDetail(FAKE_CONVERSATION_ID);
    await expect(page.getByText(/^greeting$/i)).toBeVisible();
  });

  test("transcript shows model used on bot message", async ({ page }) => {
    await injectDetailFault(page);
    const conv = new ConversationsPage(page);
    await conv.gotoDetail(FAKE_CONVERSATION_ID);
    await expect(page.getByText(/claude-haiku-3/i)).toBeVisible();
  });

  test("transcript shows response latency on bot message", async ({ page }) => {
    await injectDetailFault(page);
    const conv = new ConversationsPage(page);
    await conv.gotoDetail(FAKE_CONVERSATION_ID);
    await expect(page.getByText(/412 ms/i)).toBeVisible();
  });

  test("transcript shows RAG chunk count on bot message", async ({ page }) => {
    await injectDetailFault(page);
    const conv = new ConversationsPage(page);
    await conv.gotoDetail(FAKE_CONVERSATION_ID);
    await expect(page.getByText(/2 chunks/i)).toBeVisible();
  });

  test('"Reply in inbox" external link is present and points to chat', async ({
    page,
  }) => {
    await injectDetailFault(page);
    const conv = new ConversationsPage(page);
    await conv.gotoDetail(FAKE_CONVERSATION_ID);
    await expect(conv.replyInInboxButton).toBeVisible();
    const href = await conv.replyInInboxButton.getAttribute("href");
    expect(href).toMatch(/chat\.getpolaris\.my/);
    await expect(conv.replyInInboxButton).toHaveAttribute("target", "_blank");
  });

  test("back link returns to /conversations", async ({ page }) => {
    await injectDetailFault(page);
    const conv = new ConversationsPage(page);
    await conv.gotoDetail(FAKE_CONVERSATION_ID);
    await expect(conv.backLink).toBeVisible();
    await expect(conv.backLink).toHaveAttribute("href", "/conversations");
  });

  test("no whitelabel leak on detail page", async ({ page }) => {
    await injectDetailFault(page);
    const conv = new ConversationsPage(page);
    await conv.gotoDetail(FAKE_CONVERSATION_ID);
    await conv.assertNoBrandLeak();
  });
});

// ---------------------------------------------------------------------------
// 404 — missing conversation
// ---------------------------------------------------------------------------

test.describe("Conversation 404 [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("non-existent conversation_id renders 404 page", async ({ page }) => {
    // The SSR server component calls notFound() when the real API returns 404.
    // No mock needed — the fake UUID simply does not exist in the DB.
    const missingId = "00000000-0000-0000-0000-000000000404";
    await page.goto(`/conversations/${missingId}`);
    await page.waitForLoadState("networkidle");
    // Next.js notFound() renders the built-in 404 page.
    await expect(page.getByText(/not found|404/i).first()).toBeVisible();
  });
});
