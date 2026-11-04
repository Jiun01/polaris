/**
 * Typed Polaris API route paths (Phase 8 PR-3).
 *
 * Single source of truth for every `/api/tenant/*` URL the portal
 * touches. Centralises pluralisation choices + query-string composition
 * so a rename on the API side is a one-file diff.
 *
 * Endpoints are documented in `docs/Polaris V2/05_DATA_MODEL_AND_API.md §6.2`.
 */

export const TenantRoutes = {
  me: () => "/api/tenant/me",
  dashboardSummary: () => "/api/tenant/dashboard/summary",
  settings: () => "/api/tenant/settings",

  notifications: (params?: {
    cursor?: string;
    since?: string;
    page_size?: number;
    unread_only?: boolean;
  }) => {
    const q = new URLSearchParams();
    if (params?.cursor) q.set("cursor", params.cursor);
    if (params?.since) q.set("since", params.since);
    if (params?.page_size !== undefined)
      q.set("page_size", String(params.page_size));
    if (params?.unread_only) q.set("unread_only", "true");
    const s = q.toString();
    return s ? `/api/tenant/notifications/?${s}` : "/api/tenant/notifications/";
  },
  notification: (id: string) => `/api/tenant/notifications/${id}`,
  notificationsMarkAllRead: () => "/api/tenant/notifications/mark-all-read",

  // Phase 8 PR-4..PR-10 — registered upfront so additions don't churn
  // call sites later. Implementations land with their respective PRs.
  channels: () => "/api/tenant/channels",
  widget: () => "/api/tenant/widget",
  conversations: () => "/api/tenant/conversations",
  conversation: (id: string) => `/api/tenant/conversations/${id}`,
  /** List with a pre-built query string (caller owns URLSearchParams). */
  conversationsList: (qs: string) => `/api/tenant/conversations/?${qs}`,
  knowledge: () => "/api/tenant/knowledge",
  knowledgeCapacity: () => "/api/tenant/knowledge/capacity",
  knowledgeRequests: (limit = 50) =>
    `/api/tenant/knowledge/requests?limit=${limit}`,
  knowledgeRequest: (id: string) => `/api/tenant/knowledge/requests/${id}`,
  knowledgeSignUpload: () => "/api/tenant/knowledge/uploads/sign",
  /**
   * Optional windowDays: 7 | 30 | 90. Omitting keeps server default (30).
   * Back-compat: existing bookmarks / callers that omit the arg still work.
   */
  knowledgeGapsSummary: (windowDays?: 7 | 30 | 90) =>
    windowDays !== undefined
      ? `/api/tenant/knowledge-gaps/summary?window_days=${windowDays}`
      : "/api/tenant/knowledge-gaps/summary",
  /** Drill-down: contributing conversations for a given normalized question. */
  knowledgeGapsExamples: (q: string, windowDays: number) =>
    `/api/tenant/knowledge-gaps/summary/examples?q=${encodeURIComponent(q)}&window_days=${windowDays}`,
  // NOTE: trailing slash. Every existing caller (templates/page.tsx,
  // TemplateForm.tsx, broadcasts/new/page.tsx) already posts/gets with the
  // slash; this constant previously had none and simply had zero callers
  // (verified during audit-batch-f-frontend-consolidation F3 — grepped
  // every TenantRoutes.templates() call site before changing the return
  // value, per the plan's gotcha). Safe to correct directly rather than
  // add a second templatesCreate() entry.
  templates: () => "/api/tenant/templates/",
  audience: () => "/api/tenant/audience",
  /** List with a pre-built query string (caller owns URLSearchParams). */
  audienceList: (qs: string) => `/api/tenant/audience/?${qs}`,
  audienceOptedOut: () => "/api/tenant/audience/opted-out",
  /** List with a pre-built query string (caller owns URLSearchParams). */
  audienceOptedOutList: (qs: string) => `/api/tenant/audience/opted-out?${qs}`,
  /** Browser-navigation CSV download (plain <a href>, not apiFetch). */
  audienceOptedOutCsv: () => "/api/tenant/audience/opted-out.csv",
  audienceImport: () => "/api/tenant/audience/import",
  audienceImportFromConversations: (sinceDays: number) =>
    `/api/tenant/audience/import-from-conversations?since_days=${sinceDays}`,
  // Same trailing-slash correction as templates() above — zero callers of
  // the no-slash form existed; both broadcasts/page.tsx (GET) and
  // BroadcastWizard.tsx (POST) already use the slash form.
  broadcasts: () => "/api/tenant/broadcasts/",
  promotions: () => "/api/tenant/promotions/",
  promotion: (id: string) => `/api/tenant/promotions/${id}`,
  branches: () => "/api/tenant/branches/",
  branch: (id: string) => `/api/tenant/branches/${id}`,
  bookingsConfig: () => "/api/tenant/bookings/config",
  bookingsConfigSync: () => "/api/tenant/bookings/config/sync",
  followupsConfig: () => "/api/tenant/followups/config",
  billingInvoices: () => "/api/tenant/billing/invoices",
  billingUsage: () => "/api/tenant/billing/usage",
  // Same trailing-slash correction as templates()/broadcasts() above — grep
  // (2026-07-02, frontend-chips-followup) found ZERO callers of the no-slash
  // form; both real call sites (billing/page.tsx, PlanChangeForm.tsx) and the
  // e2e mocks (e2e/features/billing.spec.ts) already use the slash form.
  planChangeRequests: () => "/api/tenant/plan-change-requests/",
  privacyExport: () => "/api/tenant/privacy/export",
  privacyDelete: () => "/api/tenant/privacy/delete",

  products: () => "/api/tenant/products/",
  product: (id: string) => `/api/tenant/products/${id}`,
  productImages: (id: string) => `/api/tenant/products/${id}/images`,
  productImage: (id: string, imageId: string) =>
    `/api/tenant/products/${id}/images/${imageId}`,

  knowledgeSources: () => "/api/tenant/knowledge/sources",
  knowledgeSource: (id: string) => `/api/tenant/knowledge/sources/${id}`,
} as const;
