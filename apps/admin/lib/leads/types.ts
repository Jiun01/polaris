/**
 * Website-leads helper types (admin /leads surface).
 *
 * HAND-MAINTAINED BY DESIGN, not a TODO: re-confirmed during
 * audit-batch-f-frontend-consolidation (2026-07) that no `WebsiteLead*`
 * Pydantic model exists in packages/shared-types — website_leads.py's
 * `_WebsiteLeadItem`/`_WebsiteLeadsResponse` are router-private and were
 * never exported there. Mirrors that router's response shape; keep in
 * sync manually. Re-check
 * packages/shared-types/python/polaris_shared_types/ before adding a new
 * hand-maintained duplicate elsewhere.
 */

export type LeadFormType = "demo" | "pricing" | "contact" | "website";
export type LeadStatus = "new" | "contacted";

export interface WebsiteLead {
  id: string;
  form_type: LeadFormType;
  name: string;
  email: string;
  phone: string | null;
  business_name: string | null;
  industry: string | null;
  message: string | null;
  details: Record<string, unknown>;
  status: LeadStatus;
  contacted_at: string | null;
  created_at: string;
}

export interface WebsiteLeadsResponse {
  items: WebsiteLead[];
  next_cursor: string | null;
}
