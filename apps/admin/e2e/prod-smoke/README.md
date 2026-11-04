# Prod smoke suite (admin)

Real Playwright tests against the **live production** admin app
(`https://admin.getpolaris.my`) — not the local/CI E2E suite, which mocks
routes and depends on MailHog. This suite uses a real login session and hits
real endpoints. See `../docs/internal-ops/PRPs/plans/branch-product-promo-demo-remediation.plan.md`
and the delegated plan for the v1.0.80 + EA r19 verification context.

## Safety

- Never expect the gateway tenant's delete/purge to succeed — only that it's
  blocked.
- Any tenant this suite creates is prefixed `qa-smoke-` and torn down in
  `afterEach`/`afterAll`. If a run crashes mid-way, grep the admin Tenants
  list for that prefix and clean up by hand.
- Never touch a real paying-customer tenant.

## One-time auth setup

Real magic-link email can't be intercepted automatically here (no MailHog on
prod). Capture a session once, reuse it until it expires:

```bash
cd apps/admin
PROD_SMOKE=1 PLAYWRIGHT_ADMIN_BASE_URL=https://admin.getpolaris.my \
  npx playwright open --project=prod-smoke https://admin.getpolaris.my/login
```

1. Enter your platform-admin email, submit.
2. Check your real email, click the magic link (in the SAME opened browser).
3. Once you land on an authenticated admin page, run in the Playwright
   Inspector's console (or a one-off script):
   ```js
   await page.context().storageState({ path: "e2e/.auth/prod-admin.json" });
   ```
4. Re-run this whenever `prod-admin.json` expires (auth failures in the
   suite are the signal).

## Running

```bash
cd apps/admin
PROD_SMOKE=1 PLAYWRIGHT_ADMIN_BASE_URL=https://admin.getpolaris.my \
  npx playwright test --project=prod-smoke
```

`PROD_SMOKE=1` disables the local suite's MailHog-based `globalSetup` for
this run — required, or it will try (and fail) to sign in via MailHog.
