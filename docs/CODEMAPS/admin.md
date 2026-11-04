
# Admin Portal Codemap (apps/admin)

## Page Tree

```
app/
  page.tsx                          → redirect → /tenants
  (auth)/
    login/page.tsx                  → magic-link form
    verify-request/page.tsx         → "check your email"
  (admin)/layout.tsx                → sidebar + topbar (Caddy basic-auth gate)
    tenants/
      page.tsx                      → tenant list (search, filter by tier/status)
      new/page.tsx                  → create wizard (name + tier + admin email → magic-link)
      [tenant_id]/page.tsx          → tenant detail (Overview/Channels/Lifecycle tabs)
      [tenant_id]/delete/page.tsx   → hard-delete confirm + DSAR cascade
    quotas/
      page.tsx                      → all-tenants usage table (bot replies, seats, tier)
      [tenant_id]/page.tsx          → per-tenant override — 6 tabs, inline save, enterprise warning (v1.0.14)
    secrets/
      page.tsx                      → tenant list → link to per-tenant secrets
      [tenant_id]/page.tsx          → Meta tokens + Telegram bot token + email IMAP/SMTP
    billing/
      page.tsx                      → invoice list (overdue highlighted)
      new/select/page.tsx           → pick tenant to invoice
      new/[tenant_id]/page.tsx      → invoice form (WeasyPrint PDF)
      [invoice_id]/page.tsx         → invoice detail + mark-paid
    plan-changes/
      page.tsx                      → pending plan_change_requests queue
      [request_id]/page.tsx         → approve / reject wizard
    quotes/
      page.tsx                      → sales quote list
      new/page.tsx                  → quote builder
    audit/
      page.tsx                      → audit log viewer (hash-chain)
      verify/page.tsx               → on-demand re-verify
    impersonation/page.tsx          → audit-logged impersonation flow
    system-health/page.tsx          → Supabase/Redis/queue/Meta headroom/webhook errors (5-min refresh)
    financials/page.tsx             → MRR overview (big number + 30d trend)
    cost/
      page.tsx                      → per-tenant LLM cost overview
      [tenant_id]/page.tsx          → cost breakdown (Anthropic/Cohere/OpenRouter)
    channels/page.tsx               → channel overview (read-only)
    promotions/page.tsx             → promotions management
    demo-system/
      page.tsx                      → Demo System list (all demo configs + active session counts)
      new/page.tsx                  → create demo (brand name + demo code + duration/message caps)
      [demo_id]/page.tsx            → demo detail (edit config, sessions table, reset-visitor)
```

## Key Components

```
components/quotas/
  UsageTable.tsx     → all-tenants table (usage bars, tier badges, Override link)
  OverrideForm.tsx   → tabbed per-field override (v1.0.14):
                       Tabs: Bot Replies | Channels & Features | Storage & Content |
                             Rate Limits & Tokens | Campaigns & Bookings | Seats
                       Each field: label + description + current value + inline Save
                       Boolean fields: Enabled/Disabled select
                       allowed_channel_kinds: read-only badges (array, requires migration)
components/tenants/
  TenantDetailTabs.tsx      → Overview / Channels / Lifecycle tabs
  ChatwootAccountField.tsx  → inline chatwoot_account_id edit (v1.0.13)
  AdminDeleteSection.tsx    → soft/hard delete cards
  AddChannelModal.tsx       → v1.0.20 (#88): token_preview (masked last-4, read-only) 
                             + Telegram inbox-id field moved to top + per-channel status note
                             v1.0.19: token-capture status note + toast feedback
  RemoveChannelButton.tsx   → v1.0.22: per-channel "Remove" in Channels tab (confirm →
                             DELETE /tenants/{id}/channels/{kind} → router.refresh);
                             hidden during impersonation. Inverse of AddChannelModal.
components/secrets/
  EmailConfigSection.tsx    → read-only IMAP/SMTP config + PasswordUpdatePanel
  RotationForm.tsx          → v1.0.20 (#88): each rotation option labeled with actual credential
                             (WhatsApp WABA token / FB Page token / Telegram bot token / CW agent token)
```

## API Call Pattern

- Server components: `adminServerFetch<T>(route)` — cookie-forwarded JWT
- Client components: `adminFetch<T>(route, { method, body })` — same-domain JWT
- Routes in `lib/api/routes.ts`

Key routes:
  adminRoutes.quotas.usage()        → GET  /api/admin/quotas/usage
  adminRoutes.quotas.override(id)   → PUT  /api/admin/quotas/{id}/override
  adminRoutes.tenants.list()        → GET  /api/admin/tenants
  adminRoutes.tenants.addChannel(id)            → POST   /api/admin/tenants/{id}/channels
  adminRoutes.tenants.removeChannel(id, kind)   → DELETE /api/admin/tenants/{id}/channels/{kind}
  adminRoutes.secrets.get(id)       → GET  /api/admin/secrets/{id}
  adminRoutes.demos.list()          → GET  /api/admin/demos
  adminRoutes.demos.create()        → POST /api/admin/demos
  adminRoutes.demos.update(id)      → PATCH /api/admin/demos/{id}
  adminRoutes.demos.delete(id)      → DELETE /api/admin/demos/{id}
  adminRoutes.demos.sessions(id)    → GET  /api/admin/demos/{id}/sessions
  adminRoutes.demos.resetVisitor(id) → POST /api/admin/demos/{id}/reset-visitor
  adminRoutes.health.snapshot()      → GET  /api/admin/health
