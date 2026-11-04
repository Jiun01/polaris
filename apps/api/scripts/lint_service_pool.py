"""CI lint — forbid `get_service_pool` imports outside the allowlist.

Per ADR-002: `service_pool` connects with BYPASSRLS and is reserved for cron,
admin (`/api/admin/*`), migrations, and other paths the SOC review explicitly
sanctions. Every other code path uses `anon_pool` so RLS is the structural
floor on cross-tenant leakage.

This script greps `apps/api/polaris_api/` for `get_service_pool` references
outside the allowlist directories. Exits 0 if clean, 1 if any usage is found
outside the allowlist (with the offending file:line printed).

Invoked from CI and from `apps/api/tests/test_pools_lint.py` (so a misuse
breaks both local pytest and GitHub Actions in lockstep).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Allowlist — paths (relative to apps/api/polaris_api/) where get_service_pool
# may be referenced. Add to this list ONLY after a security review note in the
# corresponding PR.
ALLOWED_PREFIXES: tuple[str, ...] = (
    "db/pools.py",                       # defines + exports get_service_pool — must reference itself
    "main.py",                           # lifespan KMS sentinel verify (PR-3) — platform-only by design
    "services/admin/",                   # /api/admin/* routes (Phase 6+)
    "services/webhook_resolver.py",      # Phase 2 PR-2 — webhook tenant resolution happens BEFORE we
                                         # have a TenantContext (the request is unauthenticated by
                                         # design — Meta/Chatwoot just POST). Only this module may
                                         # use the BYPASSRLS pool; the webhook router calls into it
                                         # via its narrow public surface (ResolvedChannel + resolve_*
                                         # + record_event) and never imports get_service_pool itself.
    "services/demo_sessions.py",         # Phase 2 PR-6 / ADR-066 — demo session primitives + demo-code →
                                         # hidden brand-tenant resolution. Demo brand tenants are hidden
                                         # platform tenants the operator owns; resolving a demo_code →
                                         # brand tenant (and reading the brand's caps/display_name) is a
                                         # cross-tenant lookup no single-tenant RLS scope can satisfy, so
                                         # it uses the BYPASSRLS pool. Read paths + the session upsert.
    "services/demo_runtime.py",          # ADR-066 (Demo System v3) — the demo decision state machine
                                         # reads the brand tenant's display_name (for the localized expiry
                                         # message) via the service pool. Cross-tenant read with no
                                         # TenantContext (the active session names the brand tenant); same
                                         # platform-level posture as demo_sessions.py. Read-only.
    "services/pipeline.py",              # ADR-066 (Demo System v3) — SOLE sanctioned use is
                                         # _resolve_channel_owner: a brand-demo turn scopes content to the
                                         # hidden brand tenant but must dispatch the reply via the GATEWAY
                                         # channel's creds. Resolving the channel's owner tenant from
                                         # channel_id is a READ-ONLY lookup the brand-scoped RLS would
                                         # hide, so it uses the service pool; creds are then loaded under
                                         # the owner's own RLS scope. No mutation via the service pool.
    "services/templates/lifecycle.py",   # Phase 6 PR-3 — Meta template-status webhook arrives
                                         # unauthenticated (HMAC-only); the handler does a
                                         # cross-tenant meta_template_id -> tenant_id resolution
                                         # before any TenantContext exists, then sets
                                         # app.current_tenant_id explicitly via set_config before
                                         # mutating. Same posture as services/webhook_resolver.py.
    "services/broadcast/worker.py",      # Phase 6 PR-6 — long-running drain loop runs OUTSIDE any
                                         # request context. The worker scans the per-tenant ZSET via
                                         # the service pool, claims one broadcast per tick per tenant,
                                         # then enters _tenant_scoped_conn(tenant_id) which EXPLICITLY
                                         # sets app.current_tenant_id via set_config before any audit
                                         # / compliance / recipient mutation. RLS-scoped queries flow
                                         # through downstream services unchanged. Per-tenant
                                         # isolation invariant is enforced by the set_config guard
                                         # at the top of every batch context manager.
    "services/followups/worker.py",      # Phase 13 — AI follow-ups drain loop runs OUTSIDE any request
                                         # context (lifespan tick worker), same posture as
                                         # broadcast/worker.py: it scans due followup_jobs via the
                                         # service pool, then sets app.current_tenant_id explicitly
                                         # before any tenant-scoped read/write. Per-tenant isolation is
                                         # enforced by the set_config guard at the top of each batch.
    "services/email_assist.py",          # Email channel M2 — bot-assist draft + acceptance tracking run
                                         # OUTSIDE any request context (drained from a lifespan queue;
                                         # record_agent_reply is invoked from the Chatwoot webhook AFTER
                                         # HMAC verify + tenant resolution). Every query is explicitly
                                         # tenant_id-scoped (search_hybrid / load_tenant_snapshot take a
                                         # tenant_id arg; email_drafts is filtered by tenant_id and is
                                         # itself service-scoped — GRANT polaris_service only).
    "services/email_assist_worker.py",   # Email channel M2 — lifespan tick worker draining the email-
                                         # assist queue (cross-tenant), same posture as followups/worker.py:
                                         # acquires the service pool once and delegates each job to
                                         # email_assist (above), which scopes every query by tenant_id.
    "services/broadcast/delivery.py",    # Phase 6 PR-7 — wa-status webhook arrives unauthenticated
                                         # (HMAC-only); handler resolves wamid -> tenant_id cross-
                                         # tenant via the service pool, then sets
                                         # app.current_tenant_id explicitly before mutating
                                         # broadcast_recipients + broadcasts counters. Same posture
                                         # as templates/lifecycle.py.
    "services/channels/lazy_provision.py",  # Phase 10 — first-inbound auto-provision is dispatched from
                                         # the Chatwoot webhook AFTER HMAC verify, outside any request-
                                         # scoped TenantContext (the webhook arrives unauthenticated by
                                         # design). It resolves chatwoot account_id -> tenant cross-tenant
                                         # (no single-tenant RLS scope can satisfy this) and provisions the
                                         # tenant_channels row + captures the token. Same posture as
                                         # services/webhook_resolver.py; the router calls this narrow
                                         # surface (lazy_provision_channel) and never imports the pool.
    "services/chatwoot/reverse_handoff.py",  # Phase 7 PR-5 — reverse-handoff is dispatched from the
                                         # Chatwoot webhook handler AFTER HMAC verify + tenant
                                         # resolution. It runs outside any request-scoped TenantContext
                                         # and needs the service pool to set app.current_tenant_id
                                         # before clearing handoff_at + querying the conversations
                                         # table. Explicit set_config guards every tenant-scoped query.
    "webhooks/wa_account_router.py",     # Phase 6 PR-7 — MM Lite onboarding webhook resolves
                                         # phone_number_id -> tenant_channels row across tenants
                                         # (phone_number_id is Meta-globally-unique), then sets
                                         # app.current_tenant_id before the UPDATE on
                                         # tenant_channels.mm_lite_*. Mirrors the
                                         # webhook_resolver.py + templates/lifecycle.py posture for
                                         # unauthenticated-arrival webhooks.
    "jobs/",                             # cron entrypoints (Phase 4+ retention sweeper, billing flusher)
    "routers/admin_audit.py",            # Phase 9 PR-5 — POST /verify triggers background run_verification;
                                         # the background task needs the pool directly (not via FastAPI dep)
                                         # because BackgroundTasks runs after the response is sent and the
                                         # Depends lifecycle has already closed. Admin-only; platform_admin
                                         # auth guard enforced before the task is scheduled.
    "routers/admin_impersonation.py",   # Phase 9 PR-5 — impersonation start/stop need the pool to write
                                         # audit rows (start_impersonation + stop_impersonation). Both
                                         # endpoints are gated by require_platform_admin. The service pool is
                                         # needed because these writes carry both actor_id and
                                         # impersonating_id — the anon pool's RLS would reject the write
                                         # since the impersonation session has no set_config tenant_id yet.
    "scheduled/",                        # Phase 9 PR-5 — nightly APScheduler job passes the service pool
                                         # to run_verification; runs outside any request context.
    "services/booking/link_flow.py",     # v2.1 — booking-via-EA-link. create_pending_booking runs
                                         # inside the inbound pipeline (no request TenantContext) and
                                         # confirm_booking / sweep run from the EA webhook + a periodic
                                         # task (both unauthenticated-arrival, like webhook_resolver).
                                         # Every query is explicitly tenant_id-scoped.
    "services/booking/branch_config.py", # v2.1 — branch EA-config read from the booking node inside the
                                         # inbound pipeline (no TenantContext). Read is scoped by both
                                         # conversation_id AND tenant_id; no cross-tenant surface.
    "routers/booking_redirect.py",       # phase-13 — public GET /b/{jti} short-link redirect. Customer has
                                         # no Polaris session; scoped by the unguessable 32-hex jti
                                         # (binding_token) + status='pending' + unexpired. Read-only.
    "services/meta_usage.py",            # System Health (2026-06-01) — best-effort writer for the
                                         # platform-internal meta_api_usage telemetry table (migration
                                         # 085). Invoked as a fire-and-forget sink from the meta_whatsapp
                                         # send path (outside any request TenantContext). The table holds
                                         # NO tenant-scoped data (just per-surface Graph API headroom), so
                                         # BYPASSRLS carries zero cross-tenant leakage surface; the writer
                                         # only INSERTs and swallows all errors.
    "services/widget.py",                # Off-Meta channels M3 — the PUBLIC website-widget config read
                                         # (get_public_config, lookup by opaque widget_key) arrives with NO
                                         # TenantContext, exactly like webhook_resolver. Read-only + keyed by
                                         # widget_key; no writes (the read-only widget links direct to t.me).
)

# A file under apps/api/polaris_api/ that mentions `get_service_pool` is a hit;
# we then check whether its path is allowlisted. Match the bare identifier so
# comments and strings still trip the gate — leaking the name into a log
# message that explains how to bypass RLS would be a security smell.
_PATTERN = re.compile(r"\bget_service_pool\b")


def _scan(root: Path) -> list[tuple[Path, int, str]]:
    """Return (path, line_number, line) hits outside the allowlist."""
    hits: list[tuple[Path, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if any(rel == p.rstrip("/") or rel.startswith(p) for p in ALLOWED_PREFIXES):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _PATTERN.search(line):
                hits.append((path, i, line.rstrip()))
    return hits


def main() -> int:
    # Resolve relative to this script's location so the lint works whether
    # invoked from repo root, apps/api/, or anywhere else.
    here = Path(__file__).resolve()
    root = here.parent.parent / "polaris_api"
    if not root.exists():
        print(f"lint_service_pool: {root} not found", file=sys.stderr)
        return 1
    hits = _scan(root)
    if not hits:
        return 0
    print(
        "lint_service_pool: `get_service_pool` referenced outside the allowlist:",
        file=sys.stderr,
    )
    for path, line_no, line in hits:
        print(f"  {path}:{line_no}: {line}", file=sys.stderr)
    print(
        "\nIf this usage is intentional, add the directory to ALLOWED_PREFIXES in\n"
        "apps/api/scripts/lint_service_pool.py with a one-line PR justification.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
