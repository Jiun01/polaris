# apps/portal — Polaris customer portal

Next.js 14 (app-router) with `output: 'standalone'` for slim Docker images.
Tenant-facing self-serve: dashboard, conversations, knowledge base, AI agent
persona, broadcasts, audience, bookings, promotions, billing, settings.

## Dev

```bash
# From repo root
pnpm install
pnpm --filter @polaris/portal dev   # http://localhost:3000
```

## Build / type-check

```bash
pnpm --filter @polaris/portal build
pnpm --filter @polaris/portal typecheck
```

## Conventions

- API paths via `lib/api/routes.ts` (`TenantRoutes`) — no string literals.
- Channel vocabulary via `lib/channel-kinds.ts`.
- Auth is NextAuth magic-link; cookies are minted for `portal.localhost` via
  the Caddy dev proxy.
