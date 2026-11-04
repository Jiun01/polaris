# Prod smoke suite (portal)

Real Playwright tests against the **live production** portal
(`https://portal.getpolaris.my`), logged in as the gateway/dogfood
tenant admin — the operator's own live-channel tenant (internal
traffic only, confirmed safe to temporarily reconfigure for these tests).
Not the local/CI E2E suite (mocked routes, MailHog, fixture tenants).

## Safety

- Every mutating spec restores original state in `afterAll`/`afterEach`
  (agent-tool toggles, channel→branch assignment) and deletes anything it
  created (branches prefixed `QA-Smoke-`).
- Live Telegram specs (`agent-tools.spec.ts`, `branch-routing.spec.ts`) send
  real messages into the conversation identified by `CW_ACCOUNT_ID` /
  `CW_CONVERSATION_ID` — the same conversation `../docs/internal-ops/ops/e2e/bot_reply` already
  uses as the designated safe-to-test channel.
- `ea-booking.spec.ts` never submits a real appointment — the public booking
  form is Cloudflare Turnstile-gated; this suite only checks the page
  structurally renders after the restyle.

## Required environment

```bash
CHATWOOT_AGENT_API_TOKEN=<super-agent token>      # to inject/poll the safe-to-test conv
CHATWOOT_BASE_URL=https://chat.getpolaris.my       # default if unset
CW_ACCOUNT_ID=1                                    # default if unset
CW_CONVERSATION_ID=16                              # default if unset
SMOKE_TENANT_SLUG=<gateway tenant slug>           # checked on the admin gateway page
SMOKE_BOOKING_URL=<public booking wizard URL>     # e.g. https://<calendar-host>/book/<slug>
```

`SMOKE_TENANT_SLUG` / `SMOKE_BOOKING_URL` default to placeholder values — set
them to the live gateway tenant's real values before running against prod
(the real values live in the operator's private runbook, not in this repo).

Specs that need Telegram skip themselves (`test.skip`) if
`CHATWOOT_AGENT_API_TOKEN` is unset, so the rest of the suite still runs.

## One-time auth setup

```bash
cd apps/portal
PROD_SMOKE=1 PLAYWRIGHT_PORTAL_BASE_URL=https://portal.getpolaris.my \
  npx playwright open --project=prod-smoke https://portal.getpolaris.my/login
```

1. Log in as the gateway tenant admin via the real magic link.
2. Once authenticated:
   ```js
   await page
     .context()
     .storageState({ path: "e2e/.auth/prod-tech-tenant.json" });
   ```
3. Re-capture whenever the session expires.

## Running

```bash
cd apps/portal
PROD_SMOKE=1 PLAYWRIGHT_PORTAL_BASE_URL=https://portal.getpolaris.my \
  CHATWOOT_AGENT_API_TOKEN=... \
  npx playwright test --project=prod-smoke
```
