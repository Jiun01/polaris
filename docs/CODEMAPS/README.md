
# Codemaps — Polaris

Token-lean architecture snapshots. **Latest verified (2026-07-02)**: migrations 001–120 on main (next=121; 120 = audit-remediation indexes, Supabase Cloud apply PENDING); the 2026-07-02 audit-remediation program (batches A–G) merged same day — reliability fixes, dead-code + DB hygiene, **flow_runtime fully retired (−11.6k LOC)**, hot-path resource-waste fixes, frontend consolidation (shared-types codegen fixed), god-module splits — see INDEX.md HEAD tier for the batch map. Migrations 118/119 ship the **knowledge_sources reconciler redesign** (declarative desired-state ledger replacing the imperative `ingestion_requests` queue — see `docs/SSOT/features/Knowledge Sources Reconciler.md`), shipped v1.0.78. **Also live on main (not previously reflected here)**: lazy channel auto-provisioning (`services/channels/lazy_provision.py`, wired into `webhooks/chatwoot_router.py`) — no longer branch-only. **Prior on main**: Chatwoot-native inbound rehaul (PR #102) — inbound flipped to Meta→Chatwoot→agent-bot→Polaris; `meta_router` deleted; legacy FB-page instagram dropped; the AI Agent portal screen; agent tool surface (PR #99) + cost metrics (PR #98) + HUMAN_AGENT tag (PR #100) + booking notifications (PR #97) + Phase 15 product catalog + AI follow-ups + Telegram/widget/email/voice/KGD. **Also merged same week (2026-07-03, not previously reflected here):** a RAG eval harness (unrun baseline), the Sonnet 4.6→5 model bump (thinking pinned off, hard tier), a TenantRoutes/admin-billing UI completion sweep, a 13-fix zero-behavior-change simplify pass, and an SMTP MailHog-tolerance fix — see SHIPPED_FEATURES.md §2026-07-03 Post-Remediation Chips for detail.

**Quick start:** Read **[INDEX.md](INDEX.md)** first for phase timeline + status matrix + 45-service overview, then drill into domain codemaps.

## Reading Order (10 minutes)

1. **[INDEX.md](INDEX.md)** (NEW) — Phase timeline + status + service domains + scheduled jobs + quick reference
2. **[SERVICES.md](SERVICES.md)** (NEW) — All 45+ service modules documented with branch status
3. **[architecture.md](architecture.md)** — Dual-pool RLS, tenant lifecycle, E2E test infrastructure
4. **[backend.md](backend.md)** — All routers (39 total), Phase 15 products + voice M1 + KGD M1 + email channel, Phase 20 Meta mock, scheduled jobs (7 total, all locked via ADR-062)
5. **[data.md](data.md)** — migrations 001–120 (next=121) + schema invariants + FK relationships
6. **[integrations.md](integrations.md)** — Chatwoot v4.14.0-r1, EA v1.6.0-r3, LLM providers, Meta/Telegram APIs, webhook contracts
7. **[frontend.md](frontend.md)** — Portal + Admin app-router structure
8. **[dependencies.md](dependencies.md)** — Pins + PyJWT 2.x + pytest session loop
9. **[admin.md](admin.md)** — Admin app specifics, E2E helpers
10. **[portal.md](portal.md)** — Portal app + NextAuth magic-link
11. **[billing.md](billing.md)** — Invoices, quotes, MRR, cost tracking, Supabase Storage

Each file: ≤1500 tokens (except INDEX/SERVICES which are reference docs)

## When to regenerate

Regenerate the codemaps after:

- Any phase closes
- Major refactor that moves files between layers
- New external service integrated
- Dependency major-version bump
- Migration count changes by >2

## Don't edit manually

These files are regenerated from source on each phase boundary. Hand edits will be overwritten. For prose that should survive regeneration, keep it out of the generated files.
