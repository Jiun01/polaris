
# Frontend — `apps/portal` + `apps/admin` (Next.js 14)

Two Next.js 14 app-router apps, both standalone-output, both behind Caddy. **Portal** = tenant self-serve at `portal.getpolaris.my` (shipped Phase 8). **Admin** = internal ops at `admin.getpolaris.my` (Phase 9 PRs #47-#52 merged; quotas, secrets, audit viewer, impersonation added).

## Shared-Types Consumption Fixed (audit Batch F, 2026-07-02)

The Pydantic→JSON-Schema→TS codegen pipeline (`packages/shared-types/scripts/export.py`)
had a `$defs`-hoisting bug: when bundling every model into one combined schema, a
sub-model's nested `$defs` entries were landing at `$defs.<Model>.$defs.<Nested>` while the
`$ref` strings inside still pointed at `#/$defs/<Nested>` (resolving against the combined
document root — a path that didn't exist). Fixed by hoisting each model's nested `$defs`
entries up to the shared top-level `$defs` dict before stripping the now-empty nested key.
`packages/shared-types/typescript/polaris-shared-types.ts` is now correctly generated AND —
new this pass — actually imported by both apps via `@polaris/shared-types` (`package.json`
dependency in both `apps/portal` and `apps/admin`). Confirmed live consumers: portal
`lib/types/conversations.ts` (NEW), `components/knowledge-gaps/{KnowledgeGapsList,
KnowledgeGapsContent}.tsx`, `components/dashboard/widgets.tsx`; admin `lib/types/{health,
billing}.ts`, `lib/impersonation/audit-helpers.ts`.

**New: `apps/portal/lib/channel-kinds.ts`** — canonical channel-kind vocabulary, closing the
drift risk flagged in SHIPPED_FEATURES.md's Instagram-Login retro ("a new channel touches
~10+ hand-rolled collections across api+portal+admin"). Consumed by 11 portal call sites +
admin's `OverrideForm` (Quotas page `allowed_channel_kinds` editor).

**`TenantRoutes` fixes (`apps/portal/lib/api/routes.ts`):** `templates()` and `broadcasts()`
were missing their trailing slash — every real call site (TemplateForm, broadcasts/new,
BroadcastWizard) already posted WITH the slash, so this had zero live callers of the buggy
no-slash form; corrected in place rather than adding a second entry. `products()` /
`productImages()` / `productImage()` / `knowledgeSources()` entries confirmed present
(Phase 15 + Knowledge Sources Reconciler routes) — all 4 predate the 2026-07-03 11-builder
sweep documented below and are unaffected by it. **`BroadcastWizard.tsx` payload bug
fixed** — the wizard's submit body was previously malformed/incomplete; now posts a
complete payload to `TenantRoutes.broadcasts()`.

**Admin cleanup:** dead `components/ui/dropdown-menu.tsx` removed (confirmed gone from
disk); `react-hook-form` + `@hookform/zod` pruned from `apps/admin/package.json` (unused
deps). **Portal cleanup:** dead `components/ui/{tooltip,skeleton}.tsx` removed. Admin
audit-verify page: hex-suffix field-name fix (cosmetic display bug, no data change).

## TenantRoutes completion sweep + admin billing load-more (2026-07-03, commit `882e043`/`dcf2c41`, follow-up to Batch F)

A follow-up chip closed the remaining hand-rolled `/api/tenant/*` string literals Batch F's
sweep missed: 11 NEW `TenantRoutes` builders added to `apps/portal/lib/api/routes.ts` —
`widget`, `conversation`, `conversationsList`, `audienceList`, `audienceOptedOutList`,
`audienceOptedOutCsv`, `audienceImport`, `audienceImportFromConversations`,
`bookingsConfigSync`, `followupsConfig`, `knowledgeSource` — consumed across 27 call sites
in 19 portal files (byte-identical paths, zero behavior change). `planChangeRequests()`
corrected to the trailing-slash form (zero callers of the no-slash form existed).

**Admin billing "Load more" (same commit cluster):** `apps/admin/components/billing/InvoiceTable.tsx`'s
PDF link — previously a raw `/api/admin/billing/invoices/{id}/pdf` href that 404'd in-browser
(not a Next.js route) — now uses `adminRoutes.billing.invoicePdf()`, the same same-origin PDF
streamer `InvoiceDetailPage` already used. NEW `apps/admin/components/billing/InvoiceTableWithLoadMore.tsx`
(client wrapper owning cursor state, appends pages via `adminFetch`) + wiring in
`apps/admin/app/(admin)/billing/page.tsx` — the active tab filter (status/year_month) is built
once server-side and re-sent on every load-more request so pagination stays inside the filter;
button hides when `next_cursor` is null. `InvoiceTable.tsx` itself stays presentational
(sort-only) — its client-side `[...items].sort()` was also wrapped in `useMemo` keyed on
`[items, sortKey, sortDir]` (previously recomputed every render).

**Shared cursor-pagination hook (2026-07-03, commit `a98ec2f`):** NEW
`apps/admin/lib/hooks/useCursorLoadMore.ts` (57 lines) — generic `items`/`cursor`/`loading`
state machine + concurrent-fetch guard, extracted from `AuditTable.tsx` and
`InvoiceTableWithLoadMore.tsx` (each had hand-rolled the identical trio). URL construction
(endpoint, filter querystring, cursor param name) stays with each caller via a `fetchPage`
callback param — the hook owns only the generic state, not the request shape. Both
`AuditTable.tsx` and `InvoiceTableWithLoadMore.tsx` now consume it; no behavior change to
either component's pagination semantics.

## Portal (Phase 8 close — see `portal.md` for the full map)

Full surface map lives in `portal.md`. Highlights:

- Magic-link auth via NextAuth EmailProvider (Phase 8 PR-2; migration 052 + `lib/auth/{options,adapter,db,email-template}.ts`; Phase 10 v1.0.53: portal auth DB now has circuit breaker on transient failures + graceful ServiceUnavailable fallback)
- 16 sidebar screens (see `portal.md` for the authoritative count) + 3 auth screens + onboarding wizard + (suspended) lockdown. **v1.0.55 (AI Agent screen redesign):** `components/agent/AgentForm.tsx` rewritten — persona block grid (2-up, named blocks with display_name/slug/body), recommended-block palette dropdown, guardrail cards (read-only, was raw XML), instructions textarea removed from UI (backend `agent_config.instructions` kept for back-compat). `components/settings/SettingsForm.tsx` += `PersonaBlockValue` / `personaBlocksSchema` / `slugifyBlockName` / `RESERVED_BLOCK_SLUGS`. `lib/settings/load.ts` carries `persona_blocks` + `default_persona_blocks` fields. **KGD interactivity (2026-06-14):** `/knowledge-gaps` gained 7/30/90-day window selector + `KnowledgeGapsList` client island with sort, signal-type filter, and lazy per-theme drill-down via the new `/summary/examples` endpoint. **9 portal QA fixes (2026-06-14):** dashboard rows→Link, template inline validation, billing Enterprise guard, promo date-range guard, bookings step=1, conversations instagram filter, checkbox→Switch (FollowupSettingsCard/BookingConfigForm/WidgetClient), bookings admin link→support note, help dropdown dead link removed.
- Flow-builder UI REMOVED (PR #99, ADR-063) — replaced by the tool-calling agent surface (`/agent` screen + `agent_config`); `flows/`, `flow-editor/`, `lib/flow-editor/` deleted.
- shadcn/ui primitives baseline in `components/ui/` — mirrored by admin in PR-3. **v1.0.76:** portal restyled to Chatwoot "next" design system: `n-*` 12-step RGB color scales (`n-slate`, `n-blue`, …) + named typography utilities in `app/globals.css`; shadcn HSL tokens re-pointed to Woot blue #2781F6 in `tailwind.config.ts`; Inter via `next/font`; `components/ui/{button,input,card,textarea,select}.tsx` rebuilt; `components/shell/Logo.tsx` + `public/brand/polaris-logo.svg` added. Admin app not changed — it keeps its own slate/HSL theme independently.

## Admin (Phase 9 in progress — PRs #47/#49/#50 stacked)

```
apps/admin/                                    ← internal ops (port 3001)
├── app/
│   ├── layout.tsx                             — sonner Toaster + "Polaris Admin" metadata
│   ├── globals.css                            — shadcn slate theme (HSL vars; admin-independent; portal diverged at v1.0.76)
│   ├── page.tsx                               — redirect → /tenants
│   ├── (auth)/                                ← PR-2 (magic-link entry)
│   │   ├── layout.tsx                         — centered card, "Polaris Admin" brand
│   │   ├── login/page.tsx                     — email entry; callbackUrl sanitized
│   │   └── verify-request/page.tsx            — "check your inbox" + IP-bind reminder
│   ├── (admin)/                               ← PR-3 (post-auth)
│   │   ├── layout.tsx                         — topbar + sidebar + main
│   │   ├── tenants/                           ← Deliverable #1
│   │   │   ├── page.tsx                       — list + status filter
│   │   │   ├── new/page.tsx                   — 4-step CreateTenantWizard host
│   │   │   ├── [tenant_id]/
│   │   │   │   ├── page.tsx                   — 5-tab detail (overview/channels/templates/branches/lifecycle)
│   │   │   │   └── delete/page.tsx            — DeleteConfirmation host (type-slug gate)
│   │   ├── promotions/page.tsx                — read-only stub
│   │   └── channels/page.tsx                  — read-only stub
│   ├── api/
│   │   ├── auth/[...nextauth]/route.ts        ← PR-2 (IP capture → withRequestIp → NextAuth)
│   │   ├── billing/invoices/[id]/pdf/         — server-side PDF proxy (mints JWT → streams FastAPI 307)
│   │   ├── proxy/[...path]/                   — generic admin→FastAPI reverse proxy (bearer forwarding)
│   │   └── quotes/[id]/pdf/                   — server-side quote PDF proxy (same pattern as billing)
├── middleware.ts                              ← PR-2 (auth guard only at PR-3 close)
├── components/
│   ├── ui/                                    — 8 shadcn primitives (button, card, badge,
│   │                                            separator, dialog, dropdown-menu, input, label)
│   ├── shell/
│   │   ├── EnvironmentBadge.tsx               — prod/staging/preview/dev color-coded
│   │   ├── ImpersonationBanner.tsx            — always-on banner (reads pol_impersonation cookie)
│   │   ├── Sidebar.tsx                        — 4 nav groups, 13 nav items, active highlight
│   │   ├── StopImpersonationButton.tsx        — DELETE /api/admin/impersonate
│   │   └── TopBar.tsx                         — brand + env badge + email + sign-out form
│   └── tenants/
│       ├── AddChannelModal.tsx                — Phase 10 retrofit; POST …/tenants/{id}/channels
│       ├── ChannelsTabActions.tsx             — action buttons on tenant detail Channels tab
│       ├── CreateWizard.tsx                   — "use client"; profile → plan → admin → confirm
│       │                                        (plan step: channel-entitlement checkboxes,
│       │                                         tier-default reset, sends allowed_channel_kinds
│       │                                         override only when diverged — LIVE on main)
│       ├── DeleteConfirmation.tsx             — type-slug gate + cascade-scope readout
│       ├── SuspendButton.tsx                  — suspend/unsuspend form action
│       ├── TenantsTable.tsx                   — status pill / identity / tier / MRR / activity
│       └── types.ts                           — TenantSummary, TenantDetail, etc. (hand-rolled
│                                                until shared-types pipeline regenerates)
├── lib/
│   ├── auth/                                  ← PR-2
│   │   ├── options.ts                         — NextAuth config; signIn + jwt (IP-bind) + session
│   │   ├── adapter.ts                         — custom adapter → platform_admins + admin_magic_link_tokens
│   │   ├── db.ts                              — pg pool against POLARIS_DB_URL_SERVICE
│   │   ├── config.ts                          — env helpers (10-min expiry, 3 req/hr, /24, 1-day session)
│   │   ├── email-template.ts                  — Polaris Admin-branded magic-link mail
│   │   ├── ip-bind.ts                         — LRU keyed by email; /24 IPv4 prefix compare
│   │   └── request-ip.ts                      — AsyncLocalStorage propagates click IP to jwt;
│   │                                            X-Polaris-Caddy-Token shared-secret check
│   ├── rate-limit/
│   │   └── magic-link.ts                      — 3 req/hour/email LRU (stricter than portal's 5)
│   ├── api/                                   ← PR-3
│   │   ├── client.ts                          — browser fetch; credentials:'include' (auth bridge
│   │   │                                        gap shared with portal — tracked follow-up)
│   │   ├── server.ts                          — server fetch; reads getServerSession
│   │   ├── routes.ts                          — typed admin API paths (centralised)
│   │   └── error.ts                           — ApiError + PolarisErrorEnvelope shape
│   ├── impersonation/
│   │   ├── audit-helpers.ts                   — audit row helpers for impersonation events
│   │   └── start.ts                           — cookie-set + redirect logic for impersonate start
│   ├── types/
│   │   ├── billing.ts                         — billing-domain TS types (invoices, archive)
│   │   └── health.ts                          — health snapshot TS types
│   ├── format.ts                              — formatMyr / formatMyt / formatInt / costBandClass
│   └── utils.ts                               — cn() helper (verbatim from portal)
├── types/next-auth.d.ts                       ← PR-2 — single platform_admin_id claim
└── components.json                            — shadcn config (slate baseColor; admin-independent; portal diverged at v1.0.76)
```

## Admin nav surface (per wireframe brief + PR-3/4/5)

Sidebar groups + items (left rail):

```
Configure  → Tenants ✓ / Quotas ✓ / Secrets ✓ / Promotions ✓(stub) / Channels ✓(stub)
Monitor    → System Health / Cost / Financials
Operate    → Billing ✓ / Plan Changes ✓ / Sales Quotes ✓ / Audit Log ✓
Compliance → Impersonation ✓
```

✓ = all shipped. Additional routes not in sidebar: `/system-health` (alias page), `/cost/[tenant_id]` (per-tenant cost drilldown).

## Auth model summary (PR-2)

Per ADR-030 Amendment 2026-05-20: magic-link only inside the app, 4 compensating controls:

1. Caddy basic-auth outer gate (`{$CADDY_ADMIN_BASIC_AUTH_HASH}`) on `admin.getpolaris.my`
2. 10-min magic-link expiry (vs portal 15) + 3 req/hr/email (vs portal 5)
3. IP-bind: click IP must share `/24` with sign-in-request IP; LRU keyed by email; propagated via AsyncLocalStorage
4. 1-day session (vs portal 30 days) + `X-Polaris-Caddy-Token` shared-secret header check to defend against direct-to-process spoofing

Custom NextAuth v4 adapter mirrors portal's pattern but targets `platform_admins` (migration 054) + `admin_magic_link_tokens` (migration 055).

## Tenant lifecycle UX (PR-3)

- **Create** → 4-step wizard; submit POSTs `/api/admin/tenants`; backend creates tenants + tenant_quotas + tenant_billing + auth.users + tenant_users + audit; magic-link dispatch via `services/portal_invite.py` (empty SMTP config → silent no-op with audit). Success toast distinguishes "sent" vs "skipped, direct user to /login".
- **Detail** → 5-tab URL-driven (`?tab=overview|channels|templates|branches|lifecycle`); Lifecycle tab carries suspend/unsuspend form + Hard-delete CTA.
- **Suspend / Unsuspend** → form-action POSTs; backend audit-records `tenant.suspended` / `tenant.unsuspended`.
- **Soft-delete** (per ADR-029) → type-slug gate + cascade-scope readout; backend flips `tenants.status = 'deleted'` + `suspended_reason = 'hard_delete_pending'` + emits `tenant.dsar_deleted` audit row. Physical purge runs in Phase 10 retention sweeper.

## Quotas + Secrets (PR-4)

**Quotas page** (`/quotas`):
- `UsageTable`: all-tenants usage with live % + filter by plan_tier
- `OverrideForm`: per-tenant override (16 columns like `bot_replies`, `outbound_broadcast_enabled`, etc.) at `/quotas/[tenant_id]`. **[LIVE on main]** `allowed_channel_kinds` now EDITABLE (was read-only "requires a DB migration") — pill toggles + dedicated `saveChannels()` → `PUT …/override {column:"allowed_channel_kinds", value:string[]}`
- Reads `/api/admin/quotas/usage` (GETable paginated set)
- Writes via `PUT /api/admin/quotas/{tenant_id}/override` (audit-logged)

**Secrets page** (`/secrets/[tenant_id]`):
- RotationForm: dropdown (meta_whatsapp | meta_ig | meta_messenger | chatwoot) + password input
- RotationHistory table: past rotations with timestamps
- Calls `POST /api/admin/secrets/{tenant_id}/rotate` (no plaintext echoed back)
- Backend wires Chatwoot fork admin API for chatwoot service

## Audit Log + Chain Verification (PR-5)

**Audit Log page** (`/audit`):
- Cursor-paginated table: actor + action + before/after JSONB + timestamp + impersonation columns
- Searches via `GET /api/admin/audit/logs?cursor=<uuid>&limit=50`
- Filter by action, tenant, date-range (frontend only at GA)

**Verification page** (`/audit/verify`):
- Status banner showing latest verification (Nightly 04:00 MYT scheduled check)
- Breakdown: rows_scanned, duration, status (valid | broken), broken_at_id (if any)
- OnDemandReverify button → `POST /api/admin/verify` (runs in background; status polls `/verifications/latest`)

**Impersonation Banner** (always-on in portal):
- Shows when `pol_impersonation` cookie present
- Displays target user name + "Stop Impersonation" button
- Stop button → `DELETE /api/admin/impersonate` (clears cookie + audit-logs)

## Impersonation modal (PR-5 admin pre-flight)

**PreImpersonationModal** (`components/impersonation/`):
- Modal triggered by admin clicking a tenant user row
- Displays user email + last sign-in
- Confirm → `POST /api/admin/impersonate/{tenant_id}/{user_id}` → sets HttpOnly cookie
- Redirect to portal at `portal.getpolaris.my` (pre-set via browser cookie scope `.getpolaris.my`)
- Portal displays impersonation banner immediately on next load

## Billing Ops (PR-7)

**Billing page** (`/billing`):
- InvoiceTable: paginated list of invoices (status pill, tenant, amount, due date, actions)
- Filters: status, tenant, month range
- Actions: View PDF, Mark Paid, Void, Archive
- CTA: "Issue Invoice" button → `/billing/new/select` (tenant picker)

**Issue Invoice flow** (`/billing/new/select` + `/billing/new/[tenant_id]`):
- Tenant selector (autocomplete)
- IssueInvoiceForm: kind (monthly|annual|plan_change_proration|addon_seat), period, subtotal, tax, due date
- Submit → `POST /api/admin/billing/invoices` → POL number + PDF render + signed URL
- Toast: "Invoice POL-M-260521-0001 issued"

**Invoice detail** (`/billing/[invoice_id]`):
- Displays full details: number, tenant, period, amount, status, PDF link
- MarkPaidButton (idempotent): calls `POST /{id}/mark-paid` if status='issued'
- Archive controls: ArchiveMonthPicker for bulk-view

**Archive view** (`/billing` → Archive tab):
- ArchiveMonthPicker: year/month selector
- Table: aggregates by (year_month, tenant_id) — counts issued/paid/overdue/void

## Plan-Change Wizard (PR-8)

**Plan Changes page** (`/plan-changes`):
- RequestQueue: pending/approved/rejected tabs (driven by status filter)
- Table: tenant, from→to tier, proration preview, decision note, actor, timestamp
- Row click → detail page

**Plan Change detail** (`/plan-changes/[request_id]`):
- Shows current tier, target tier, cycle dates, proration breakdown
- ProrationRecomputeForm: if status='pending', displays live proration math (Decimal.ROUND_HALF_UP)
- Approve button → `POST /{id}/approve` (audit-logs approval)
- Reject button → decision_note required, → `POST /{id}/reject`
- Post-approval: plan_tier flipped server-side; admin sees confirmation

## Sales Quotes (PR-9)

**Quotes page** (`/quotes`):
- QuoteList: quote_code (POL-Q-...), prospect email/company, tier, expires_at, status (draft|converted)
- Filters: status, month
- Row click → detail OR generate form link

**Quote Generator** (`/quotes/new`):
- QuoteGeneratorForm: prospect email, company, target tier, billing cycle, intended signup date
- Live client-side proration preview (two-tier pricing frame per PRD §12.4.2)
- Submit → `POST /api/admin/quotes` → render PDF + upload → display signed URL
- Toast: "Quote POL-Q-260521-0001 generated, expires 2026-06-20"

**Quote PDF preview** (`/quotes/new` inline):
- PdfPreview component: embeds signed PDF URL in iframe
- Adjusts height for A4 aspect ratio (210mm×297mm)
- Auto-fetches whenever form data changes (debounced)

**Quote detail + conversion** (`/quotes/[id]` — route not yet scaffolded; convert endpoint wired in `routes.ts`):
- Planned: display PDF, prospect info, proration inputs (JSON inspector)
- "Convert to Invoice" CTA → modal confirms target tenant + triggers `POST /api/admin/quotes/{id}/convert/{tenant_id}`
- Back-end: creates tenant_invoice row, flips tenants.plan_tier, writes audit, returns invoice PDF URL

## Known gaps (documented, follow-ups)

- **Auth bridge (Critical)** — pre-existing portal pattern: admin `lib/api/{client,server}.ts` send `credentials: 'include'` + `X-Polaris-Admin-Id` header to an API whose `deps/tenant.py::_extract_bearer` only accepts `Authorization: Bearer`. Both apps need a unified JWT-bridge in a coordinated follow-up.
- **Hash-compat smoke** — `portal_invite.py` mints sha256(token+secret) hashes intended to match NextAuth v4 EmailProvider; no automated round-trip test gates deploys. Add before first prod tenant create.
- **Portal middleware status='deleted'** — portal-side one-line follow-up: the existing ADR-049 suspension takeover should also block on `status='deleted'`.
- **Shared-types pipeline** — the codegen bug is fixed and the generated file is now consumed in several places (see §Shared-Types Consumption Fixed above), but this is NOT a full sweep: admin's `components/tenants/types.ts` (and other pre-existing hand-rolled type files not touched by audit Batch F) remain hand-rolled; migrating them to `@polaris/shared-types` is still a later-PR follow-up.

## Phase 9 PR cadence (status)

All PRs merged. Full surface shipped:

| PR | Surface | Status |
|---|---|---|
| PR-0 #47 | Deep-dive + checklist + ADR-030 Amendment | **merged ✓** |
| PR-1 #48 | Migration 054 + 7 Pydantic shapes + admin env scaffold | **merged ✓** |
| PR-2 #49 | NextAuth EmailProvider + IP-bind defence + Caddy basic-auth | **merged ✓** |
| PR-3 #50 | Admin shell + Tenants CRUD + soft-delete + magic-link dispatch | **merged ✓** |
| PR-4 | Quotas override + Secrets rotation | **merged ✓** |
| PR-5 | Audit/Impersonation | **merged ✓** |
| PR-6 | System Health dashboard | **merged ✓** |
| PR-7 | Billing Ops | **merged ✓** |
| PR-8 | Plan-Change Wizard | **merged ✓** |
| PR-9 | Sales Quotes | **merged ✓** |
| PR-10 | Cost dashboard + per-tenant drilldown | **merged ✓** |
| PR-11 | Financials / MRR tripwire | **merged ✓** |

## E2E suite + SSR fault hook (2026-05-30, PRs #71–#76)

- Suite: portal `apps/portal/e2e/{features,pages,api,journeys,cross-path,ea,_support}`;
  admin `apps/admin/e2e/{features,pages,helpers,_support}` — per-page POMs + shared fixtures;
  phase-smoke specs retired to `_legacy/` (ignored via `testIgnore`).
  Base URLs: portal `http://localhost:3000` (NextAuth origin), admin `http://admin.localhost`.
- **SSR fault hook:** `lib/api/server.ts::apiFetchServer` (portal) reads an `e2e_fault` cookie
  (gated on `POLARIS_E2E_INTERNAL_SECRET`) to force 503 / empty / canned-body on server-component
  fetches — `page.route()` can't intercept SSR. Helper `e2e/_support/e2e-fault.ts::setE2EFault`.
- Flow editor (`components/flow-editor/{FlowEditor,Canvas,NodePalette,Inspector,TopBar}`) E2E-tested
  by feeding a canned flow via the fault hook (publish-gating, validator banner, test-dialog).
