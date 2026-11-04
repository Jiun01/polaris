# Portal codemap (v1.0.76 — Chatwoot design-system restyle)


The Polaris tenant portal at `apps/portal/`. Next.js 14 app-router,
TypeScript 5.5, Tailwind 3.4 with shadcn primitives, Zustand for the
flow editor's local state, NextAuth (EmailProvider) for magic-link
auth.

**Phase 9 fix (PR-13)**: Portal proxy route (`apps/portal/app/api/tenant/[...path]/route.ts`) now handles 307 Temporary Redirect responses from the API by manually following them instead of passing the redirect status to the browser. This fixed smoke-test failures where the portal's tenant context lookup was hitting redirects.

## Route map

```
app/
├── (auth)/                            # PR-2 — no sidebar, centered card
│   ├── login/                         #   magic-link email entry
│   ├── verify-request/                #   "check your inbox"
│   └── verify/[token]/                #   token redemption
├── (portal)/                          # PR-3 — sidebar + topbar shell
│   ├── dashboard/                     #   Screen #1 — quota dials, recent, sparkline
│   ├── conversations/                 # PR-6 — Screen #2
│   │   └── [conversation_id]/         #     transcript with bot telemetry
│   ├── agent/                         # NEW (2026-06-12) — AI Agent: persona + tool-calling + read-only guardrails (nav 2nd)
│   ├── knowledge/                     # PR-6 — Screen #3 (Sources/Runs/Chunks tabs)
│   │   ├── runs/
│   │   └── chunks/[doc_id]/
│   ├── knowledge-gaps/                # KGD M3+interactivity — window=7|30|90 selector (URL param);
│   │                                  #   lazy per-theme drill-down → /conversations/{id}
│   ├── flows/                         # PR-7 — Screen #4
│   │   └── [flow_id]/                 #     xyflow canvas editor
│   ├── templates/                     # PR-8 — Screen #5
│   │   └── new/                       #     3-step compose
│   ├── audience/                      # PR-8 — Screen #6 (3 tabs)
│   │   ├── opted-out/                 #     GA-blocking PDPA viewer
│   │   └── import/                    #     CSV upload + sync
│   ├── broadcasts/                    # PR-9 — Screen #7 (Pro+ gated)
│   │   ├── new/                       #     3-step wizard
│   │   └── locked/                    #     Starter upgrade card
│   ├── channels/                      # PR-5 — Screen #8
│   ├── bookings/                      # PR-8 — Screen #9
│   ├── promotions/                    # PR-9 — Screen #11
│   │   ├── new/
│   │   └── [promo_id]/
│   ├── products/                      # Phase 15 — Screen #16 (catalog + photo gallery)
│   │   ├── new/
│   │   └── [product_id]/              #     edit + multipart image upload/delete
│   ├── branches/                      # PR-9 — Screen #12 (Pro+ gated)
│   ├── billing/                       # PR-10 — Screen #13
│   │   └── plan-change/               #     tier picker + proration
│   ├── settings/                      # PR-4 — Screen #15
│   │   └── dsar/{export,delete}/      #     PDPA flows
│   ├── onboarding/                    # PR-5 — first-run wizard
│   └── notifications/                 # PR-11 — full-page feed
├── (suspended)/                       # PR-2 — payment-only chrome
│   └── pay/                           #     ADR-049 lockdown
└── api/auth/[...nextauth]/            # PR-2 — NextAuth handler
```

## Component map

```
components/
├── ui/                                # shadcn primitives (PR-3)
├── shell/                             # Sidebar, TopBar, MobileNav, TenantBadge,
│                                      #   NotificationBell, HelpDropdown, nav-items,
│                                      #   TierLockRedirect, TierUnlockRedirect,
│                                      #   ImpersonationBanner (PR-3, PR-9, PR-11)
│                                      #   Logo.tsx (v1.0.76) — plain <img> light/dark swap;
│                                      #     assets: public/brand/polaris-logo.svg + -dark.svg;
│                                      #     middleware matcher excludes /brand so SVGs load
│                                      #     on unauthenticated pages (login/verify-request)
│                                      #   ServiceUnavailable.tsx (v1.0.53) — DB circuit-open
│                                      #     page; uses n-amber-3/n-amber-11 from n-* token system
├── providers/tenant-provider.tsx      # React Context for tenant claims (PR-3)
├── dashboard/widgets.tsx              # QuotaDial, RecentConversations,
│                                      #   QuotaSparkline, NumericTile,
│                                      #   SuspensionBanner + count tiles (PR-3)
├── conversations/                     # ChannelFilterChips, ConversationsTable,
│                                      #   ConversationTranscript (PR-6)
├── knowledge/                         # KnowledgeTabs, KnowledgeSourcesTable,
│                                      #   CapacityBar, AddKnowledgeButton (PR-6)
├── knowledge-gaps/                    # KnowledgeGapsContent (server-renderable delegate),
│                                      #   KnowledgeGapsList (NEW: client island — sort +
│                                      #   signal-type filter + lazy drill-down per theme),
│                                      #   KnowledgeGapsTierGuard
├── flow-editor/                       # Canvas, NodePalette, Inspector, TopBar,
│                                      #   FlowEditor, CreateFlowClient (PR-7)
│   └── nodes/index.tsx                #   9 ADR-050 node renderers, FLOW_NODE_TYPES
│                                      #   (start/end/trigger/llm_reply/send_template/
│                                      #    action_tag/condition/handoff/booking) (PR-7)
├── channels/                          # ChannelsClient, ChannelTile (PR-5)
├── templates/TemplateForm.tsx         # 3-step Meta-submit form (PR-8)
├── audience/                          # AudienceTabs, AudienceImportClient (PR-8)
├── bookings/BookingConfigForm.tsx     # tenants.settings.booking_config form (PR-8)
├── broadcasts/BroadcastWizard.tsx     # 3-step composer (PR-9)
├── promotions/PromotionForm.tsx       # create/edit shared form (PR-9)
├── products/ProductForm.tsx           # create/edit + image gallery (multipart upload/delete) (Phase 15)
├── branches/                          # BranchesClient, BranchesStarterPlaceholder,
│                                      #   TierGuardWrapper (PR-9)
├── billing/PlanChangeForm.tsx         # tier picker + proration display (PR-10)
├── notifications/MarkAllReadButton.tsx # full-page mark-all CTA (PR-11)
├── forms/BusinessHoursGrid.tsx        # weekly schedule editor (PR-4)
├── agent/                             # v1.0.55 redesign — AgentForm: 2-up persona block grid
│                                      #   (display_name→slug→body); recommended-block palette;
│                                      #   guardrail cards (read-only); instructions textarea removed
├── settings/                          # SettingsClient, SettingsForm,
│                                      #   DsarExportClient (PR-4),
│                                      #   FollowupSettingsCard (Phase 13)
│                                      #   (AgentSection MOVED OUT → /agent screen)
├── suspended/                         # InvoicesList, BankTransferDetails (PR-2)
└── onboarding/                        # WizardStep, ProgressBar (PR-5)
```

## Lib map

```
lib/
├── api/                               # client.ts + server.ts + routes.ts +
│                                      #   errors.ts (PR-3)
│                                      #   routes.ts += knowledgeGapsExamples(q, windowDays)
│                                      #              + optional windowDays on knowledgeGapsSummary
├── auth/                              # options + adapter + config + db +
│                                      #   email-template (PR-2)
│                                      #   db.ts += portalQuery() wrapper + circuit breaker
│                                      #   + isTransientConnectionError() + DbUnavailableError (v1.0.53)
├── flow-editor/                       # store (Zustand) + node-schemas (zod) +
│                                      #   validator-client + types + adapter +
│                                      #   api wrappers (PR-7)
├── notifications/poll.ts              # 30s visibility-gated poll (PR-11)
├── rate-limit/magic-link.ts           # in-memory LRU bucket (PR-2)
├── tier-gate.tsx                      # <TierGate> + meetsTier helper (PR-3)
├── format.ts                          # MYR, RFC3339, percent helpers (PR-3)
└── utils.ts                           # cn() + small helpers (PR-3)
```

## Polaris API endpoints consumed

| Surface | Endpoint(s) | Phase |
|---|---|---|
| Dashboard | GET /api/tenant/dashboard/summary | 8 PR-3 |
| Conversations | GET /api/tenant/conversations(/{id}) | 8 PR-6 |
| Knowledge | GET/POST /api/tenant/knowledge/requests, GET /capacity | 3 |
| Flows | full CRUD /api/tenant/flows + /flow-templates, POST /publish; /test-as-customer DISABLED (501, CTA shown disabled — real-WA test not built) | 5 + 8 PR-7 |
| Channels | GET /api/tenant/channels | 7 + 8 PR-5 |
| Templates | GET/POST /api/tenant/templates | 6 |
| Audience | GET / + POST /import + GET /opted-out(.csv) | 6 + 8 PR-8 |
| Broadcasts | full CRUD /api/tenant/broadcasts | 6 |
| Products | full CRUD /api/tenant/products + /{id}/images upload/delete/reorder (multipart via FormData) | 15 |
| Bookings | GET/PUT /api/tenant/bookings/config | 8 PR-8 |
| Promotions | full CRUD /api/tenant/promotions | 8 PR-9 |
| Branches | full CRUD /api/tenant/branches | 8 PR-9 |
| Billing | GET /api/tenant/plan-change-requests(/{id}), POST /api/tenant/plan-change-requests | 8 PR-10 |
| Notifications | GET /api/tenant/notifications, PATCH /{id}, POST /mark-all-read | 8 PR-1 + 8 PR-11 |
| Settings | GET/PATCH /api/tenant/settings | 8 PR-4 |
| AI Agent | GET/PATCH /api/tenant/settings (agent_config + read-only default_system_prompt + agent_guardrails + persona_blocks + default_persona_blocks) | v1.0.55 |
| Follow-ups | GET/PUT /api/tenant/followups/config — AI re-engagement toggle (FollowupSettingsCard on Settings) | 13 |
| Knowledge Gaps | GET /api/tenant/knowledge-gaps/summary?window_days={7\|30\|90} (window selector); GET /api/tenant/knowledge-gaps/summary/examples?q=&window_days= (drill-down, NEW) | 2026-06-14 |

## Portal API proxy route + auth helpers

**Route**: `apps/portal/app/api/tenant/[...path]/route.ts` (Phase 9 PR-13 + Portal E2E)

The catch-all `/api/tenant/*` proxy:
1. Reads NextAuth session server-side.
2. Mints short-lived HS256 JWT via `mintApiJwt()` (same secret as FastAPI expects).
3. Forwards request method, body, query string to Polaris API.
4. Handles FastAPI 307/308 trailing-slash redirects for body-bearing requests (undici workaround).
5. Streams response back to browser with original status code.

Headers forwarded: Accept, Content-Type, Authorization Bearer token. Blocked headers: Host, Cookie, X-Forwarded-* (security + trust boundary).

**Auth adapter** (`lib/auth/adapter.ts`):
- NextAuth v4 Adapter pattern (minimal surface — EmailProvider only under JWT session strategy).
- All DB calls now routed through `portalQuery()` wrapper (v1.0.53): distinguishes transient connection errors from genuine query failures; circuit breaker trips after 5 consecutive failures, fail-fast for 20s cooldown.
- `getUserByEmail(email)` — checks auth.users + tenant_users via migration 066 SECURITY DEFINER helper. Returns null if email not provisioned (signIn callback rejects).
- `createVerificationToken(token)` — stores hashed token in magic_link_tokens table.
- `useVerificationToken(params)` — atomic DELETE-on-consume (token can only be redeemed once).
- All other methods (createUser, updateUser, etc.) throw with clear error rather than silently succeeding.
- **Transient failures** (ECONNREFUSED, ETIMEDOUT, socket errors, Postgres connection-drop states): wrapped in `DbUnavailableError`, triggering breaker counter. **Config errors** (bad password 28P01, bad SQL): propagate unchanged to surface loudly.

**Auth options** (`lib/auth/options.ts`):
- EmailProvider only + magic-link (no password signup).
- JWT session strategy (not DB-backed to avoid per-request Postgres roundtrips).
- signIn callback rejects unknown emails (ADR-012, tenant_users provisioning gate).
- JWT carries: `user_id`, `tenant_id`, `tenant_slug`, `role`, `plan_tier`, `suspended_reason`.
- resolveTenantForEmail() query: auth.users + tenant_users + tenants + tenant_quotas (single SELECT per mint).
- Middleware uses `suspended_reason` to fire payment-overdue route-takeover (ADR-049).

**Email template** (`lib/auth/email-template.ts`):
- HTML magic-link email sent via SMTP (reuses Chatwoot mailer SMTP creds per Q-PHASE8-01).

**Database helpers**: 
- **Migration 066** — `public.get_portal_user_by_email(email TEXT)` SECURITY DEFINER function. Returns (id, email) from auth.users JOIN tenant_users. Grants execute to polaris_service + polaris_anon. Revokes from PUBLIC.
- **Migration 067** — `public.get_portal_user_email_by_id(user_id UUID)` SECURITY DEFINER function. Returns email TEXT from auth.users JOIN tenant_users. Same grant/revoke pattern.

**Error handling**: `lib/api/errors.ts` parses FastAPI `{detail:[...]}` 422 validation shapes and maps to `code: "VALIDATION_ERROR"`.

## E2E suite + seed scripts

**Playwright config + specs**: Land in PR-12. Specs gate on `PLAYWRIGHT_STORAGE_STATE_*` env vars (CI supplies them via fixture seeding step); local dev runs `pnpm e2e` against a live portal at `http://localhost:3000`. See `playwright.config.ts` + `e2e/*.spec.ts`.

**E2E Portal seeding** (`apps/api/scripts/seed_e2e_portal.py`, NEW):
- Idempotent script: creates Tenant B (`e2e-bookings-b`) + tenant_quotas + portal admin `e2e-portal-b@polaris-local.dev` for multi-tenancy tests.
- Verifies Tenant A (rasa-sayang-cafe) exists from prior demo seed.
- Requires `POLARIS_DB_URL_SERVICE` (or fallbacks: `DATABASE_URL` / `SUPABASE_DB_URL`).
- Safe to re-run: uses ON CONFLICT to idempotently update status/role if rows exist.
- Portal E2E bookings multi-tenancy test needs two browser contexts (one per admin) hitting `/api/tenant/bookings/config` to verify isolation.

## Whitelabel invariants (2026-05-28)

All customer-visible text in the portal must be brand-neutral. The smoke spec (`e2e/phase-03-smoke.spec.ts`) enforces this via `assertNoWhitelabelLeak()` (regex: `chatwoot|easyappointments`) on every navigation route. `KNOWN_LEAK_ROUTES` is now empty — no exemptions remain. Internal field names (`chatwoot_inbox_id`, `chatwoot_conversation_id`) and code comments are excluded from the check (not rendered to `document.body.innerText`). The neutral phrasing agreed upon: "your messaging inbox" (links/descriptions), "agent inbox" (functional references), "inbox team ID" (form labels).

## Notable invariants

- **Auth DB circuit breaker (v1.0.53)** — Per-process state in `lib/auth/db.ts`: 5 consecutive transient connection failures trip the breaker for 20s, then all calls fail fast with `DbUnavailableError` without touching a socket. Root cause: Supabase role-password drift → InvalidPasswordError flood → IPv6 auto-ban. Breaker prevents amplification; layout renders `ServiceUnavailable` instead of `redirect("/login")` which would loop. The portal layout (`app/(portal)/layout.tsx`) catches `DbUnavailableError` on tenant-context lookup and renders the transient page. One short in-request retry (250ms backoff) before declaring failure.
- **xyflow controlled mode** (`flow-editor/store.ts`) — Zustand is the
  source of truth; xyflow consumes `nodes`/`edges` props +
  `onNodesChange`/`onEdgesChange` handlers.
- **`nodeTypes` outside render** (`flow-editor/nodes/index.tsx`) —
  module-scope export, never built inside the Canvas component.
- **Validator parity** — `lib/flow-editor/validator-client.ts` mirrors
  the server validator's load-bearing rules (start uniqueness, end
  reachability, orphans, unconnected outputs). Server remains
  authority on publish.
- **No portal-side tenant secrets** — every secret-sensitive op goes
  through Polaris API (which decrypts via PyNaCl). The portal holds
  only the NextAuth session + tenant context claims.
- **TierGate never hides features** — Starter sees the locked variant
  (`/broadcasts/locked`, `BranchesStarterPlaceholder`) so the upgrade
  path is always visible per portal_design_prompt.md.

## 9 QA fixes (v1.0.39, 2026-06-14)

Behaviour fixes — no new routes or components:
- Dashboard recent-conversation rows now `<Link>` (were plain divs)
- Template wizard: inline validation added
- Billing `PlanChangeForm`: Enterprise concierge guard
- Promotion form: date-range guard
- Bookings: hour inputs `step=1`
- Conversations: Instagram filter now includes legacy `instagram` channel kind
- `FollowupSettingsCard` / `BookingConfigForm` / `WidgetClient`: native checkbox → Radix `<Switch>`
- Bookings "Open Calendar admin" link → support note (EA 1.6 has no /admin path)
- Help dropdown: dead status link removed

## Design System (v1.0.76 — Chatwoot "next" parity)

**Two coexisting token systems** (intentional — see `app/globals.css` header comment):

### 1. shadcn HSL semantic tokens
Consumed as `hsl(var(--primary))` → Tailwind classes `bg-background`, `text-foreground`, `border-border`, etc. Re-pointed to Chatwoot values in v1.0.76:
- `--primary`: `214 91% 56%` (Woot blue #2781F6)
- `--destructive`: `348 75% 59%` (ruby-9 #e54666)
- `--warning`: `42 100% 62%` (amber-9 #ffc53d); `--success`: `173 80% 36%` (teal-9 #12a594)
- Light `--background`: `0 0% 96.9%`; dark `--background`: `240 6% 7%`

### 2. Chatwoot n-* RGB scales
Defined as space-separated raw RGB in `:root` and `.dark`, consumed via `rgb(var(--slate-9) / <alpha-value>)`. Exposed in `tailwind.config.ts` as `n-slate` / `n-blue` / `n-ruby` / `n-amber` / `n-teal` / `n-iris` / `n-gray` / `n-violet` (12 steps each, Radix-style). Additional semantic aliases: `n-brand`, `n-background`, `n-surface-2`, `n-solid-2`, `n-weak`, `n-strong`, `n-container`.

**RULE:** never mix `hsl(var(--x))` and `rgb(var(--x))` in the same var definition. Never hard-code hex in component classes.

### Typography utilities (`app/globals.css` `@layer components`)
Ported from Chatwoot fork `_woot.scss`. All use `font-sans` (resolves to Inter via `--font-inter`):
- `.text-heading-1` — `text-lg font-[520]` 24px/−0.27px
- `.text-heading-2` — `text-base font-medium` 24px/−0.27px
- `.text-heading-3` — `text-sm font-medium` 21px/−0.27px
- `.text-body-main` — `text-sm font-[420]` 21px/−0.28px
- `.text-body-para` — `text-sm font-[420]` 21px/−0.21px
- `.text-label` — `text-sm font-medium` 21px
- `.text-label-small` — `text-xs font-[440]` 16px/−0.24px
- `.text-button` — `text-sm font-[460]` 21px/−0.28px
- `.text-button-small` — `text-xs font-[440]` 18px/−0.24px

Custom font weights in `tailwind.config.ts`: `420`, `440`, `460`, `520`, `620` (Inter variable font supports all).

### Font
Inter via `next/font/google` in `app/layout.tsx` → CSS variable `--font-inter` → `fontFamily.sans`.

### Primitives (`components/ui/`)
Rebuilt in v1.0.76 on new tokens. Key patterns:
- **Button** — `rounded-lg`, `h-10` default (40px), `font-[460]`, `active:enabled:scale-[0.97]`, `brightness-110` hover on solid variants, blue focus ring
- **Input** — `rounded-lg`, `outline outline-1 outline-input` (not `border`), hover `outline-n-slate-6`, focus `outline-2 outline-ring`
- **Card** — `rounded-xl bg-card outline outline-1 -outline-offset-1 outline-border`
- **Textarea** — same outline pattern as Input
- **Select** — `rounded-lg outline outline-1`

### Chrome pages on tokens (v1.0.76)
`app/(auth)/login/page.tsx`, `app/(auth)/verify-request/page.tsx`, `app/(suspended)/pay/page.tsx`, `components/shell/ServiceUnavailable.tsx` — all now use semantic tokens (`bg-background`, `text-foreground`, `bg-destructive/10`, `n-amber-3`, `n-amber-11`) instead of hard-coded colors. Status-pill colors in data tables are intentionally kept as literal colors (not refactored).

## Where to add a new screen

1. Server Component shell at `app/(portal)/<route>/page.tsx`.
2. Client components in `components/<feature>/`.
3. Polaris API endpoint(s) in `apps/api/polaris_api/routers/`.
4. Wire the sidebar in `components/shell/nav-items.tsx`.
5. Update this codemap.
