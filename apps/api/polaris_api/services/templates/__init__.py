"""WhatsApp template lifecycle services — Phase 6 PR-3.

Two modules:

  * ``repo`` — CRUD against ``tenant_templates`` from inside a
    TenantContext transaction (polaris_anon under RLS). Pure async
    DB I/O; no HTTP calls.

  * ``lifecycle`` — translates a Meta template-status-update webhook
    payload into a status flip + audit row + outbound event row, and
    triggers the auto-pause cascade for any scheduled/running
    broadcasts referencing a degraded template (Q-PHASE6-03).

Per ADR-034 Amendment 3rd, tenants paste their own System User token;
both modules accept ``access_token`` from the caller — they never read
``tenant_secrets`` themselves.
"""
