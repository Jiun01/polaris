/**
 * PROD SMOKE — WS3: branch entry-point, end-to-end via real bot behavior.
 *
 * The audit's core finding was that `tenant_channels.tenant_branch_id` had
 * no writer anywhere. This spec proves the whole chain now works live:
 * writer (PATCH /api/tenant/channels/{id}/branch) -> conversation stamping
 * (pipeline._ensure_conversation) -> handoff.py's branch-overrides-tenant
 * read -> a REAL Telegram handoff message that matches the branch's
 * override text, not the tenant default. No DB access needed — the proof is
 * live bot behavior.
 *
 * Runs against the gateway tenant's own Telegram conversation.
 * Teardown clears the channel's branch assignment and deletes the branch,
 * regardless of pass/fail.
 */

import { test, expect } from "@playwright/test";
import { sendIncoming, setPending, waitForReply } from "./_chatwoot";

const BRANCH_NAME = `QA-Smoke-Branch-${Date.now()}`;
const OVERRIDE_AFTER_HOURS_MESSAGE =
  "QA-SMOKE: this branch is closed — a team member will follow up.";

test.describe("WS3 — branch entry-point (live handoff routing)", () => {
  let branchId: string | undefined;
  let channelId: string | undefined;
  let originalChannelBranchId: string | null = null;

  test.beforeAll(() => {
    test.skip(
      !process.env.CHATWOOT_AGENT_API_TOKEN,
      "CHATWOOT_AGENT_API_TOKEN not set — see e2e/prod-smoke/README.md",
    );
  });

  test.afterAll(async ({ browser }) => {
    const context = await browser.newContext({
      storageState: "./e2e/.auth/prod-tech-tenant.json",
    });
    const request = context.request;
    try {
      if (channelId) {
        await request
          .patch(`/api/tenant/channels/${channelId}/branch`, {
            data: { tenant_branch_id: originalChannelBranchId },
          })
          .catch(() => {});
      }
      if (branchId) {
        await request
          .delete(`/api/tenant/branches/${branchId}`)
          .catch(() => {});
      }
    } finally {
      await context.close();
    }
  });

  test("create branch, assign the Telegram channel, override business hours", async ({
    page,
    request,
  }) => {
    // Real UI action — the only field the existing form exposes.
    await page.goto("/branches");
    await page.getByLabel("Add a branch").fill(BRANCH_NAME);
    await page.getByRole("button", { name: /add branch/i }).click();
    await expect(page.getByText(BRANCH_NAME)).toBeVisible();

    const branchesRes = await request.get("/api/tenant/branches/");
    expect(branchesRes.ok()).toBeTruthy();
    const branches: Array<{ id: string; name: string }> =
      await branchesRes.json();
    const created = branches.find((b) => b.name === BRANCH_NAME);
    expect(created, "created branch not found via API").toBeTruthy();
    branchId = created!.id;

    // No portal form for these fields yet (frontend deferred) — set via the
    // same authenticated session's request context. "closed every day" makes
    // the branch permanently after-hours, so the handoff assertion below is
    // deterministic regardless of when this suite runs.
    const closedAllWeek = Object.fromEntries(
      ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].map((d) => [
        d,
        { open: "00:00", close: "00:00", closed: true },
      ]),
    );
    const patchRes = await request.patch(`/api/tenant/branches/${branchId}`, {
      data: {
        name: BRANCH_NAME,
        business_hours: closedAllWeek,
        handoff_message_after_hours: OVERRIDE_AFTER_HOURS_MESSAGE,
      },
    });
    expect(patchRes.ok()).toBeTruthy();

    const channelsRes = await request.get("/api/tenant/channels");
    expect(channelsRes.ok()).toBeTruthy();
    const channels = (await channelsRes.json()).items as Array<{
      id: string;
      channel: string;
      tenant_branch_id?: string | null;
    }>;
    const telegramChannel = channels.find((c) => c.channel === "telegram");
    expect(
      telegramChannel,
      "no Telegram channel found on this tenant",
    ).toBeTruthy();
    channelId = telegramChannel!.id;
    originalChannelBranchId = telegramChannel!.tenant_branch_id ?? null;

    const assignRes = await request.patch(
      `/api/tenant/channels/${channelId}/branch`,
      { data: { tenant_branch_id: branchId } },
    );
    expect(assignRes.ok()).toBeTruthy();
    const assigned = await assignRes.json();
    expect(assigned.tenant_branch_id).toBe(branchId);
  });

  test("a real handoff message on that channel uses the branch's override text", async () => {
    expect(
      channelId,
      "previous test must have assigned the branch first",
    ).toBeTruthy();

    await setPending();
    const msgId = await sendIncoming("I want to talk to a human, please");
    const reply = await waitForReply(msgId);
    expect(reply, "no bot reply arrived within the timeout").not.toBeNull();
    expect(reply).toContain("QA-SMOKE");
  });
});
