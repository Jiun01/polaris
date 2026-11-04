/**
 * Website leads table (client).
 *
 * Chips filter by status/form_type per the plan's filter semantics: All + the
 * type chips show status='new' (the to-do inbox); Contacted shows
 * status='contacted'. "Mark contacted" PATCHes the lead and removes it from the
 * current (new) view — it now lives under Contacted. Export downloads a CSV of
 * the active chip's filter via the same-origin /api/leads/export handler.
 */

"use client";

import { useState } from "react";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import type {
  LeadFormType,
  LeadStatus,
  WebsiteLead,
  WebsiteLeadsResponse,
} from "@/lib/leads/types";

interface Props {
  initialItems: WebsiteLead[];
  initialNextCursor: string | null;
}

interface View {
  key: string;
  label: string;
  status: LeadStatus;
  formType?: LeadFormType;
}

const VIEWS: View[] = [
  { key: "all", label: "All", status: "new" },
  { key: "demo", label: "Demo", status: "new", formType: "demo" },
  { key: "pricing", label: "Pricing", status: "new", formType: "pricing" },
  { key: "contact", label: "Contacts", status: "new", formType: "contact" },
  { key: "website", label: "Website", status: "new", formType: "website" },
  { key: "contacted", label: "Contacted", status: "contacted" },
];

const TYPE_BADGE: Record<LeadFormType, string> = {
  demo: "bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300",
  pricing: "bg-teal-100 text-teal-800 dark:bg-teal-900/30 dark:text-teal-300",
  contact: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300",
  website:
    "bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300",
};

const TYPE_LABEL: Record<LeadFormType, string> = {
  demo: "Demo",
  pricing: "Pricing",
  contact: "Contact",
  website: "Website",
};

function viewQs(view: View): string {
  const params = new URLSearchParams({ status: view.status });
  if (view.formType) params.set("form_type", view.formType);
  return params.toString();
}

function fmtDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString("en-MY", {
      dateStyle: "short",
      timeStyle: "short",
    });
  } catch {
    return iso;
  }
}

function renderDetails(details: Record<string, unknown>): string {
  return Object.entries(details)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(
      ([k, v]) =>
        `${k}: ${typeof v === "boolean" ? (v ? "yes" : "no") : String(v)}`,
    )
    .join(" · ");
}

export function LeadsTable({ initialItems, initialNextCursor }: Props) {
  const [activeView, setActiveView] = useState<string>("all");
  const [items, setItems] = useState<WebsiteLead[]>(initialItems);
  const [cursor, setCursor] = useState<string | null>(initialNextCursor);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [marking, setMarking] = useState<string | null>(null);

  const current = VIEWS.find((v) => v.key === activeView) ?? VIEWS[0];
  const isContactedView = current.status === "contacted";

  async function selectView(view: View) {
    if (view.key === activeView || loading) return;
    setActiveView(view.key);
    setLoading(true);
    setError(null);
    try {
      const data = await adminFetch<WebsiteLeadsResponse>(
        `${adminRoutes.leads.list()}?${viewQs(view)}`,
      );
      setItems(data?.items ?? []);
      setCursor(data?.next_cursor ?? null);
    } catch {
      setError("Failed to load leads.");
      setItems([]);
      setCursor(null);
    } finally {
      setLoading(false);
    }
  }

  async function loadMore() {
    if (!cursor || loading) return;
    setLoading(true);
    try {
      const params = new URLSearchParams(viewQs(current));
      params.set("cursor", cursor);
      const data = await adminFetch<WebsiteLeadsResponse>(
        `${adminRoutes.leads.list()}?${params.toString()}`,
      );
      if (data) {
        setItems((prev) => [...prev, ...data.items]);
        setCursor(data.next_cursor);
      }
    } finally {
      setLoading(false);
    }
  }

  async function markContacted(id: string) {
    if (marking) return;
    setMarking(id);
    setError(null);
    try {
      await adminFetch(adminRoutes.leads.markContacted(id), {
        method: "PATCH",
      });
      // The lead moved to 'contacted' — drop it from this (new) view.
      setItems((prev) => prev.filter((l) => l.id !== id));
    } catch {
      setError("Failed to mark contacted. Please retry.");
    } finally {
      setMarking(null);
    }
  }

  return (
    <div className="space-y-3">
      {/* Chips + export */}
      <div className="flex flex-wrap items-center gap-2">
        {VIEWS.map((view) => {
          const active = view.key === activeView;
          return (
            <span key={view.key} className="flex items-center">
              {view.key === "contacted" && (
                <span className="mr-2 h-4 w-px bg-border" aria-hidden />
              )}
              <button
                onClick={() => selectView(view)}
                aria-pressed={active}
                className={
                  active
                    ? "rounded-full border border-foreground/40 bg-secondary px-3 py-1 text-xs font-medium text-secondary-foreground"
                    : "rounded-full border border-border px-3 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
                }
              >
                {view.label}
              </button>
            </span>
          );
        })}
        <a
          href={adminRoutes.leads.export(viewQs(current))}
          className="ml-auto rounded-md border border-border px-3 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          Export CSV
        </a>
      </div>

      {error && (
        <p
          role="alert"
          className="rounded-md bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-900/20 dark:text-red-300"
        >
          {error}
        </p>
      )}

      {/* Table */}
      <div className="overflow-x-auto rounded-md border border-border">
        <table className="w-full text-xs">
          <thead className="border-b border-border bg-muted/40">
            <tr>
              <th
                scope="col"
                className="px-3 py-2 text-left font-semibold text-muted-foreground"
              >
                When
              </th>
              <th
                scope="col"
                className="px-3 py-2 text-left font-semibold text-muted-foreground"
              >
                Type
              </th>
              <th
                scope="col"
                className="px-3 py-2 text-left font-semibold text-muted-foreground"
              >
                Lead
              </th>
              <th
                scope="col"
                className="px-3 py-2 text-left font-semibold text-muted-foreground"
              >
                Business
              </th>
              <th
                scope="col"
                className="px-3 py-2 text-left font-semibold text-muted-foreground"
              >
                Details
              </th>
              <th
                scope="col"
                className="px-3 py-2 text-right font-semibold text-muted-foreground"
              >
                {isContactedView ? "Contacted" : "Action"}
              </th>
            </tr>
          </thead>
          <tbody>
            {items.length === 0 && (
              <tr>
                <td
                  colSpan={6}
                  className="px-3 py-6 text-center text-muted-foreground"
                >
                  {loading ? "Loading…" : "No leads in this view."}
                </td>
              </tr>
            )}
            {items.map((lead) => (
              <tr
                key={lead.id}
                className="border-b border-border align-top hover:bg-muted/20"
              >
                <td className="whitespace-nowrap px-3 py-2 text-muted-foreground">
                  {fmtDate(lead.created_at)}
                </td>
                <td className="px-3 py-2">
                  <span
                    className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${TYPE_BADGE[lead.form_type]}`}
                  >
                    {TYPE_LABEL[lead.form_type]}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <div className="font-medium text-foreground">{lead.name}</div>
                  <div className="text-muted-foreground">{lead.email}</div>
                  {lead.phone && (
                    <div className="text-muted-foreground">{lead.phone}</div>
                  )}
                </td>
                <td className="px-3 py-2">
                  <div>{lead.business_name ?? "—"}</div>
                  {lead.industry && (
                    <div className="text-muted-foreground">{lead.industry}</div>
                  )}
                </td>
                <td className="max-w-[220px] px-3 py-2 text-muted-foreground">
                  {lead.message ? (
                    <span className="line-clamp-2">{lead.message}</span>
                  ) : (
                    renderDetails(lead.details) || "—"
                  )}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right">
                  {isContactedView ? (
                    <span className="text-green-700 dark:text-green-400">
                      {lead.contacted_at ? fmtDate(lead.contacted_at) : "—"}
                    </span>
                  ) : (
                    <button
                      onClick={() => markContacted(lead.id)}
                      disabled={marking === lead.id}
                      className="rounded-md border border-border px-2 py-1 text-xs text-foreground hover:bg-muted disabled:opacity-50"
                    >
                      {marking === lead.id ? "Marking…" : "Mark contacted"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {cursor && (
        <button
          onClick={loadMore}
          disabled={loading}
          className="w-full rounded-md border border-border py-1.5 text-xs text-muted-foreground hover:bg-muted disabled:opacity-50"
        >
          {loading ? "Loading…" : "Load more"}
        </button>
      )}
    </div>
  );
}
