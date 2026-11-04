# Polaris

Polaris is a multi-tenant AI chatbot platform for Malaysian SMEs. Each tenant
gets an AI agent that answers customers on WhatsApp, Instagram, Messenger,
Telegram, email, and a website widget, grounded in the tenant's own knowledge
base via RAG. Human handoff, bookings, and outbound campaigns are built in.

This is the monorepo that powered [getpolaris.my](https://getpolaris.my).

## Status

**Reference release.** Polaris ran in production; the deployment has since
been retired and the code is published as a portfolio piece. It's not a
supported self-hosting product: the Supabase project, container images, and
private ops docs it references are intentionally not part of the release.

## Features

- **AI agent.** Claude, tiered Haiku/Sonnet by turn complexity with an
  OpenRouter fallback. Tool calling covers knowledge search, product/promo
  lookup, booking, and handoff. Per-tenant persona blocks and guardrails;
  replies in English, Bahasa Malaysia, or Chinese.
- **RAG knowledge base.** Tenant-uploaded sources, 1024-dim embeddings
  (qwen3-embedding-8b), hybrid BM25 + vector retrieval, Cohere rerank on
  higher tiers.
- **Channels.** WhatsApp (Meta Cloud API), Instagram, Messenger, Telegram,
  shared-mailbox email, and a website widget, plus inbound voice-note
  transcription via Groq Whisper.
- **Human handoff.** Label-driven escalation into a Chatwoot agent inbox,
  business-hours aware, with recovery notes if the inbox is unreachable.
- **Bookings.** Conversational booking, cancel/reschedule, and reminders on
  the Easy!Appointments fork.
- **Outbound.** Meta template submission, audience management, broadcast
  campaigns with 24-hour-window compliance and STOP/opt-out enforcement,
  AI follow-ups.
- **SaaS plumbing.** Plan tiers and quotas, invoices and sales quotes as
  PDFs, cost dashboards, an audit hash-chain, DSAR export/delete, retention
  sweeps, per-tenant encrypted secrets.

## Architecture

```
Customer ── WhatsApp/IG/Messenger/Telegram/Email/Widget
                │
        Chatwoot fork (agent inbox + inbound relay)
                │ signed webhooks
        ┌───────▼────────┐        ┌──────────────────┐
        │   apps/api     │───────▶│ Easy!Appointments │
        │   (FastAPI)    │        │ fork (bookings)   │
        └───────┬────────┘        └──────────────────┘
                │
     Postgres 17 (Supabase, dual-pool RLS)  +  Redis (queues)
                │
     apps/portal (tenant self-serve)   apps/admin (ops console)
```

- **Dual-pool Postgres.** A `polaris_anon` (RLS-enforced) pool serves
  tenant-scoped requests; a `polaris_service` (BYPASSRLS) pool serves cron
  and admin paths. Tenant isolation is enforced at the database layer, not
  just in application code.
- **Inbound is Chatwoot-native.** Channels land in the Chatwoot fork, which
  relays signed webhooks to the API; replies dispatch back through the same
  channel creds.
- **Append-only migrations.** `packages/db-migrations/` holds 121 numbered
  SQL files, each tenant-facing table paired with a `_grants` migration.

## Repository layout

```
apps/
  api/               FastAPI backend: pipeline, RAG, webhooks, admin + tenant APIs
  portal/            Next.js 14: tenant self-serve UI
  admin/             Next.js 14: platform ops console
packages/
  shared-types/      Pydantic models → JSON Schema → TypeScript codegen
  db-migrations/     numbered .sql + idempotent apply.py
infra/               docker compose (dev + prod), Caddy, deploy scripts, dashboards
forks/               source references to the fork repos (git submodules)
  chatwoot           messaging inbox, MIT (community edition)
  easyappointments   booking engine, GPL-3.0
docs/                ENV reference, commands, codemaps (architecture docs)
evals/  benchmarks/  LLM eval + cost benchmark harnesses
scripts/             dev/demo utilities
```

Clone with submodules to include the fork sources:

```bash
git clone --recursive <repo-url>
# or, after a normal clone:
git submodule update --init
```

## Running it

This code is published **as reference**. Standing up the full stack takes
Supabase, Redis, the two fork services, and a set of provider credentials
(Meta, Telegram, Anthropic/OpenRouter, Groq, SMTP); `.env.example` documents
every variable. A reasonable starting point:

```bash
uv sync && pnpm install
cp .env.example .env        # fill in credentials
make dev                    # redis + api + portal + admin via docker compose
make verify                 # lint + typecheck + test + smoke
```

See [docs/COMMANDS.md](docs/COMMANDS.md), [docs/ENV.md](docs/ENV.md), and
[docs/CODEMAPS/](docs/CODEMAPS/) for the architecture docs. Roughly 2,000
tests sit under `apps/api/tests/` and the per-app `e2e/` suites.

> Some comments and docs reference `../docs/*` paths; those point at the
> maintainer's private workspace documentation (runbooks, decision log) and
> are intentionally not published.

## License

**Business Source License 1.1.** See [LICENSE](LICENSE). Read it, copy it,
modify it, self-host it for yourself or your own organization; all of that
is free. What the license does not allow is offering Polaris, or a
derivative of it, to third parties as a hosted, white-labeled, rebranded,
or resold service without a separate agreement. It is meant for people to
use and learn from, not to be repackaged and sold. On 2030-11-04 the license
converts to Apache-2.0 automatically.

The forks under `forks/` are modified forks of upstream projects, not
vanilla copies; they carry Polaris-specific changes (whitelabeling,
provisioning, signed webhooks, design rework). They stay under their
upstream licenses, Chatwoot community edition is MIT and Easy!Appointments
is GPL-3.0, so you are free to change and use them under those terms. The
BUSL restriction applies to the Polaris code in this repository only; it
cannot and does not extend over the forks. See [NOTICE](NOTICE).
