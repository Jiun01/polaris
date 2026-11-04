/**
 * Typed admin API route paths (Phase 9 PR-3).
 *
 * Centralised path constants + builders for `/api/admin/*`. Components
 * and Server Actions import from here so a refactor on the API side
 * surfaces as one diff, not 30. Mirrors apps/portal/lib/api/routes.ts
 * pattern.
 */

export const adminRoutes = {
  tenants: {
    list: () => "/api/admin/tenants",
    create: () => "/api/admin/tenants",
    detail: (tenantId: string) => `/api/admin/tenants/${tenantId}`,
    update: (tenantId: string) => `/api/admin/tenants/${tenantId}`,
    suspend: (tenantId: string) => `/api/admin/tenants/${tenantId}/suspend`,
    unsuspend: (tenantId: string) => `/api/admin/tenants/${tenantId}/unsuspend`,
    /** Soft-delete: flips status → 'deleted', stops bot, locks portal. */
    softDelete: (tenantId: string) => `/api/admin/tenants/${tenantId}`,
    /** Hard-delete: permanent purge. Tenant must already be soft-deleted. */
    purge: (tenantId: string) => `/api/admin/tenants/${tenantId}/purge`,
    /** Phase 10 PR-0-retrofit — ADR-015 Amendment 3rd. */
    addChannel: (tenantId: string) => `/api/admin/tenants/${tenantId}/channels`,
    /** Remove a single channel (+ its captured token) without nuking the tenant. */
    removeChannel: (tenantId: string, channelKind: string) =>
      `/api/admin/tenants/${tenantId}/channels/${channelKind}`,
    /** Off-Meta channels M2 — Telegram bot provisioning. */
    connectTelegram: (tenantId: string) =>
      `/api/admin/tenants/${tenantId}/channels/telegram`,
    /** Chatwoot inbox metadata lookup for admin channel-add modal auto-fill. */
    inboxInfo: (tenantId: string, inboxId: number) =>
      `/api/admin/tenants/${tenantId}/channels/inbox-info?chatwoot_inbox_id=${inboxId}`,
    /** Read-only IMAP/SMTP config for all email inboxes in the tenant's CW account. */
    emailConfig: (tenantId: string) =>
      `/api/admin/tenants/${tenantId}/channels/email-config`,
    /** Update IMAP or SMTP password for an email inbox via Chatwoot. */
    emailCredentials: (tenantId: string, inboxId: number) =>
      `/api/admin/tenants/${tenantId}/channels/email-credentials/${inboxId}`,
    /** Set or update the tenant's Chatwoot account ID. */
    chatwootAccount: (tenantId: string) =>
      `/api/admin/tenants/${tenantId}/chatwoot-account`,
    /** Create the tenant's Easy!Appointments account (admin + provider + service). */
    createEaAccount: (tenantId: string) =>
      `/api/admin/tenants/${tenantId}/ea-account`,
    /** Deprovision the tenant's Easy!Appointments account. */
    deleteEaAccount: (tenantId: string) =>
      `/api/admin/tenants/${tenantId}/ea-account`,
  },
  demos: {
    list: () => "/api/admin/demos",
    create: () => "/api/admin/demos",
    detail: (demoId: string) => `/api/admin/demos/${demoId}`,
    update: (demoId: string) => `/api/admin/demos/${demoId}`,
    remove: (demoId: string) => `/api/admin/demos/${demoId}`,
    resetVisitor: (demoId: string) =>
      `/api/admin/demos/${demoId}/reset-visitor`,
    sessions: (demoId: string) => `/api/admin/demos/${demoId}/sessions`,
  },
  quotas: {
    usage: () => "/api/admin/quotas/usage",
    override: (tenantId: string) => `/api/admin/quotas/${tenantId}/override`,
  },
  secrets: {
    rotate: (tenantId: string) => `/api/admin/secrets/${tenantId}/rotate`,
  },
  audit: {
    logs: () => "/api/admin/audit/logs",
    latestVerification: () => "/api/admin/audit/verifications/latest",
    triggerVerify: () => "/api/admin/audit/verify",
  },
  impersonation: {
    start: (tenantId: string, userId: string) =>
      `/api/admin/impersonate/${tenantId}/${userId}`,
    stop: () => "/api/admin/impersonate",
  },
  health: {
    snapshot: () => "/api/admin/health",
  },
  billing: {
    invoices: () => "/api/admin/billing/invoices",
    issueInvoice: () => "/api/admin/billing/invoices",
    // Same-origin admin route (app/api/billing/invoices/[id]/pdf) — it mints the
    // platform JWT server-side, follows FastAPI's 307 to the signed URL, and
    // streams the PDF back. A direct /api/admin/... link would 404 on the admin
    // origin (that path lives on the FastAPI service, not the Next app).
    invoicePdf: (invoiceId: string) => `/api/billing/invoices/${invoiceId}/pdf`,
    markPaid: (invoiceId: string) =>
      `/api/admin/billing/invoices/${invoiceId}/mark-paid`,
    voidInvoice: (invoiceId: string) =>
      `/api/admin/billing/invoices/${invoiceId}/void`,
    archive: () => "/api/admin/billing/archive",
  },
  planChanges: {
    pending: () => "/api/admin/plan-changes",
    get: (requestId: string) => `/api/admin/plan-changes/${requestId}`,
    approve: (requestId: string) =>
      `/api/admin/plan-changes/${requestId}/approve`,
    reject: (requestId: string) =>
      `/api/admin/plan-changes/${requestId}/reject`,
  },
  quotes: {
    list: () => "/api/admin/quotes",
    create: () => "/api/admin/quotes",
    // Same-origin admin route (streams the PDF; see invoicePdf note) — a direct
    // /api/admin/... link 404s on the admin origin.
    pdf: (quoteId: string) => `/api/quotes/${quoteId}/pdf`,
    convert: (quoteId: string, tenantId: string) =>
      `/api/admin/quotes/${quoteId}/convert/${tenantId}`,
  },
  cost: {
    allTenants: () => "/api/admin/cost/tenants",
    perTenant: (tenantId: string) => `/api/admin/cost/tenants/${tenantId}`,
    cohereOverrides: () => "/api/admin/cost/cohere-overrides",
    summary: () => "/api/admin/cost/summary",
    fixed: () => "/api/admin/cost/fixed",
    fixedItem: (id: number) => `/api/admin/cost/fixed/${id}`,
  },
  financials: {
    mrr: () => "/api/admin/financials/mrr",
    recomputeMrr: () => "/api/admin/financials/mrr/recompute",
  },
  leads: {
    list: () => "/api/admin/leads",
    markContacted: (id: string) => `/api/admin/leads/${id}/contacted`,
    // Same-origin route handler that streams the CSV (the generic /api/proxy is
    // JSON-only and can't carry the CSV body — same reason as billing.invoicePdf).
    // `qs` carries the active chip's filter (status + optional form_type).
    export: (qs: string) => `/api/leads/export${qs ? `?${qs}` : ""}`,
  },
} as const;
