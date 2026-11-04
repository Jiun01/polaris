/**
 * PROD SMOKE — general deploy health regression.
 *
 * Read-only. Confirms the platform health snapshot reports every core
 * dependency healthy after the v1.0.80 + EA r19 deploy, independent of any
 * specific workstream above.
 */

import { test, expect } from "@playwright/test";

test.describe("General deploy health", () => {
  test("GET /api/admin/health reports core dependencies healthy", async ({
    request,
  }) => {
    const res = await request.get("/api/proxy/api/admin/health");
    expect(res.ok()).toBeTruthy();
    const snapshot = await res.json();

    expect(snapshot.supabase?.healthy).toBe(true);
    expect(snapshot.redis?.healthy).toBe(true);
    expect(snapshot.chatwoot?.healthy).toBe(true);
    expect(snapshot.ea?.healthy).toBe(true);
  });
});
