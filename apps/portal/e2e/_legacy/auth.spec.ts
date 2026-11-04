/**
 * Auth E2E — magic-link → verify → Dashboard (Phase 8 PR-12).
 *
 * Drives the login form, intercepts the magic-link email via MailHog
 * (CI fixture), clicks the link, lands on Dashboard. The MailHog
 * dependency is documented in `../docs/internal-ops/RUNBOOK.md §magic-link debug`.
 *
 * At local-dev without MailHog this test is skipped via the env gate.
 */

import { expect, test } from "@playwright/test";

const MAILHOG_API = process.env.MAILHOG_API_URL;

test.skip(
  () => !MAILHOG_API,
  "MAILHOG_API_URL not set — magic-link round-trip requires MailHog",
);

test("magic-link sign-in lands on the Dashboard", async ({ page, request }) => {
  await page.goto("/login");

  const email = `pytest+${Date.now()}@polaris-e2e.invalid`;
  await page.getByLabel(/email/i).fill(email);
  await page.getByRole("button", { name: /send.*link/i }).click();
  await expect(page).toHaveURL(/\/verify-request/);

  // Poll MailHog for the magic-link email.
  const link = await pollForMagicLink(request, email);
  await page.goto(link);
  await expect(page).toHaveURL(/\/dashboard/);
  await expect(page.getByRole("heading", { name: /dashboard/i })).toBeVisible();
});

async function pollForMagicLink(
  request: import("@playwright/test").APIRequestContext,
  email: string,
  timeoutMs = 30_000,
): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const res = await request.get(
      `${MAILHOG_API}/api/v2/search?kind=to&query=${email}`,
    );
    const body = (await res.json()) as {
      items: { Content: { Body: string } }[];
    };
    if (body.items.length > 0) {
      const match = body.items[0].Content.Body.match(/https?:\/\/\S+verify\S+/);
      if (match) return match[0];
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(
    `No magic-link email arrived for ${email} within ${timeoutMs}ms`,
  );
}
