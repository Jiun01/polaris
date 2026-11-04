# Codemap — Billing Suite (Phase 9 complete)


**Last Updated:** 2026-07-02 | Schema and function signatures corrected against live code; 2026-07-02 audit-remediation pass (Batch C/E) touched `admin_billing.py` only — see note below

Covers the full billing pipeline: invoice issuance, plan-change approval,
sales-quote generation, proration math, PDF rendering, and Supabase Storage.

## Architecture overview

```
Admin browser
  │
  ├─ /billing/*  → admin_billing.py router  → invoice_sequencer + pdf_renderer + storage
  ├─ /plan-changes/*  → admin_plan_changes.py router  → proration.py (shared)
  └─ /quotes/*  → admin_quotes.py router  → quote generator + proration.py (shared)

proration.py  ←── single source of truth ──→  used by billing + plan-changes + quotes
```

## API routers (apps/api/polaris_api/routers/)

| Router | Endpoints | Key behaviour |
|---|---|---|
| `admin_billing.py` | `POST /api/admin/billing/invoices` — issue; `GET` list/filter; `GET /{id}/pdf` — signed-URL redirect; `POST /{id}/mark-paid`; `GET /archive` | **PR-13**: Fixed to match migration 008 schema: `period TEXT`, `amount_myr NUMERIC` (removed period_start/period_end/subtotal_myr/tax_myr/total_myr). Calls `invoice_sequencer` → `pdf_renderer` → `storage`; mark-paid emits `invoice_paid` notification. **Audit 2026-07-02 (Batch C):** list/filter + archive queries switched to keyset pagination with explicit column lists (were offset-based / `SELECT *`) — same response shape, no route/contract change. **Audit 2026-07-02 (Batch E):** error responses at 5 call sites now use the new `utils/errors.py::api_error()` helper for the `{"detail":{"code":...}}` envelope (was ad-hoc `HTTPException(detail="...")` strings at those sites). **Frontend consumption (2026-07-03):** the admin billing UI's "Load more" (cursor pagination consuming this endpoint's keyset response shape) is documented in `frontend.md` — see "TenantRoutes completion sweep + admin billing load-more". |
| `admin_plan_changes.py` | `GET /api/admin/plan-changes` — pending queue; `POST /{id}/approve` — idempotent; `POST /{id}/reject` | Approval re-runs `proration.py` server-authoritatively; creates `tenant_invoices` row + flips PCR status |
| `admin_quotes.py` | `POST /api/admin/quotes` — generate; `GET` list; `GET /{id}/pdf` — signed-URL; `POST /{id}/convert/{tenant_id}` | **PR-13**: Fixed to match migration 054 schema: `quote_code`, `prospect_email`, `generated_by_admin_id`, `converted_to_tenant_id`. Renderer versioning (`v1`); byte-identical across re-downloads via bucket key |

## Services (apps/api/polaris_api/services/)

### `billing/proration.py` — SINGLE SOURCE OF TRUTH

```python
def compute_proration(
    current_tier: PlanTier,
    target_tier: PlanTier,
    billing_cycle: BillingCycle,
    effective_date: date,
    period_start: date,
    period_end: date,
) -> ProrationResult:
    ...
```

Used by **three callers**:
1. `admin_billing.py` — invoice issuance (kind=`plan_change_proration`)
2. `admin_plan_changes.py` — approval recompute (server-authoritative)
3. `admin_quotes.py` — sales-quote price estimate

All three must produce identical results for the same inputs (parity test in
`test_proration.py` asserts this cross-caller).

### `billing/invoice_sequencer.py`

```python
async def next_invoice_number(kind: InvoiceKind, pool) -> str:
    # SELECT invoice_sequence FOR UPDATE
    # → POL-M-YYMMINDEX (monthly) or POL-A-YYMMINDEX (annual)
```

Gap-free monotonic under concurrent inserts via `SELECT ... FOR UPDATE`.
Monthly and annual share the same sequence table but distinct `kind` columns.

### `billing/pdf_renderer.py`

```python
def render_invoice_pdf(invoice: TenantInvoice, tenant: Tenant) -> bytes:
    # Jinja2 template → WeasyPrint → bytes

def render_quote_pdf(quote: SalesQuote) -> bytes:
    # Jinja2 template → WeasyPrint → bytes
```

WeasyPrint renders A4 with embedded Inter + Noto CJK fonts (for MY-Chinese
shop display names). Font dir: `WEASYPRINT_FONT_DIR=/usr/share/fonts`.
Template versioning (`renderer_version: 'v1'` on `proration_inputs` JSONB).

### `billing/storage.py`

```python
async def upload_invoice_pdf(invoice_id: str, pdf_bytes: bytes) -> str:
    # Supabase Storage → INVOICE_PDF_BUCKET
    # Returns signed URL (TTL 60 min)

async def upload_quote_pdf(quote_id: str, pdf_bytes: bytes) -> str:
    # Supabase Storage → SUPABASE_STORAGE_QUOTES_BUCKET
    # Returns signed URL
```

Both buckets are private; service-role-key access only. Byte-identical
re-downloads guaranteed by stable bucket key (`invoices/{invoice_id}.pdf`).

## Jinja2 templates (infra/templates/)

| Template | Used for |
|---|---|
| `invoice.html.j2` + `invoice.css` | Tenant invoices (all kinds) |
| `sales_quote.html.j2` + `sales_quote.css` | Sales quotes |

Templates receive `invoice`/`quote` + `tenant` context dicts. Renderer
bumps `renderer_version` when template changes; existing PDF bytes stay
in Storage untouched.

## DB tables (migration 054)

| Table | Purpose |
|---|---|
| `tenant_invoices` | Invoice rows; `invoice_number` pattern `POL-{M|A}-YYMMINDEX` per ADR-028 |
| `invoice_sequence` | Monthly sequence counter; `SELECT FOR UPDATE` concurrency guard |
| `sales_quotes` | Quote rows; `quote_code` pattern `POL-Q-YYMMINDEX` |
| `plan_change_requests` | Extended with `decided_by_admin_id` / `decided_at` / `decision_note` |

All tables: `RLS DISABLED`, `polaris_anon` explicitly REVOKED. Service-pool only.

## Scheduled jobs

| Job | Schedule (MYT) | Module |
|---|---|---|
| `invoice_overdue` | Daily 09:00 | `scheduled/invoice_overdue.py` — flips `issued` → `overdue` on past-due invoices; emits `invoice_overdue` notification |

## Env vars

| Var | Default | Notes |
|---|---|---|
| `INVOICE_PDF_BUCKET` | `polaris-invoices` | Supabase Storage bucket for invoice PDFs |
| `SUPABASE_STORAGE_QUOTES_BUCKET` | `polaris-sales-quotes` | Supabase Storage bucket for quote PDFs |
| `WEASYPRINT_FONT_DIR` | `/usr/share/fonts` | Font search path for WeasyPrint |

## Key tests

| Test file | Coverage |
|---|---|
| `apps/api/tests/admin/test_admin_billing.py` | POL-M-YYMM-INDEX monotonic; PDF rendered + uploaded; mark-paid notification |
| `apps/api/tests/admin/test_admin_plan_changes.py` | Approval recomputes proration; idempotent retry; reject with decision_note |
| `apps/api/tests/admin/test_admin_quotes.py` | PDF byte-identical across re-downloads; convert warns on expired; quote_code unique |
| `apps/api/tests/services/test_invoice_sequencer.py` | Gap-free monotonic under 10 parallel issues |
| `apps/api/tests/services/test_pdf_renderer.py` | Snapshot-test against golden file |
| `apps/api/tests/services/test_proration.py` | **Parity gate** — billing + plan-change + quote paths all produce same result for same inputs |
