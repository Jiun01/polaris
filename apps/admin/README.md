# apps/admin — Polaris platform admin

Next.js 14 (app-router) with `output: 'standalone'`. Internal ops console for
the platform admin: tenants, quotas, secrets rotation, billing, plan changes,
sales quotes, cost/financial dashboards, audit log, impersonation, system
health, demo system.

## Dev

```bash
pnpm install
pnpm --filter @polaris/admin dev   # http://localhost:3001
```

## Conventions

- API paths via `lib/api/routes.ts` — no string literals.
- Magic-link auth with tighter controls than the portal (shorter expiry,
  IP-bind, Caddy shared-secret header).
- Audit-sensitive actions record an `audit_logs` row server-side.
