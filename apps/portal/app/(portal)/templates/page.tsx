/**
 * Templates list — Screen #5 (Phase 8 PR-8).
 *
 * Server Component. Fetches `/api/tenant/templates` and renders a
 * status-badged list with rejection_reason inline + Resubmit CTA.
 * The "New template" CTA routes to /templates/new for a multi-step
 * compose form (see new/page.tsx).
 */

import Link from "next/link";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatAbsolute } from "@/lib/format";
import { cn } from "@/lib/utils";

type TemplateStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "paused"
  | "disabled";
type TemplateCategory = "AUTHENTICATION" | "MARKETING" | "UTILITY";

interface TenantTemplateDTO {
  id: string;
  name: string;
  language: string;
  category: TemplateCategory;
  status: TemplateStatus;
  rejection_reason: string | null;
  last_status_change_at: string | null;
  submitted_at: string | null;
  created_at: string;
}

const STATUS_TONE: Record<TemplateStatus, string> = {
  pending: "bg-blue-100 text-blue-900 dark:bg-blue-950 dark:text-blue-100",
  approved:
    "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100",
  rejected: "bg-destructive/15 text-destructive",
  paused: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-100",
  disabled: "bg-muted text-muted-foreground",
};

async function loadTemplates(): Promise<{
  templates: TenantTemplateDTO[];
  error?: string;
}> {
  try {
    const res = await apiFetchServer<TenantTemplateDTO[]>(
      TenantRoutes.templates(),
    );
    return { templates: res ?? [] };
  } catch (err) {
    const code =
      err instanceof PolarisApiError
        ? `${err.status} ${err.code}`
        : String(err);
    // eslint-disable-next-line no-console
    console.error(`[portal /templates] fetch failed: ${code}`, err);
    return { templates: [], error: "Couldn't load your templates." };
  }
}

export default async function TemplatesPage() {
  const { templates, error } = await loadTemplates();

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Templates</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            WhatsApp message templates you&apos;ve submitted to Meta. Status
            updates land automatically, so there&apos;s no need to refresh.
          </p>
        </div>
        <Button asChild>
          <Link href="/templates/new">New template</Link>
        </Button>
      </header>

      {error ? (
        <div
          role="status"
          className="rounded-lg border border-warning bg-warning/10 px-4 py-3 text-sm text-warning"
        >
          {error}
        </div>
      ) : null}

      {templates.length === 0 && !error ? (
        <div className="rounded-xl border border-dashed bg-muted/30 p-8 text-center">
          <h2 className="text-base font-semibold">No templates yet</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Templates are the WhatsApp messages your bot can send to opted-in
            customers, like broadcasts, booking confirmations, and promotions.
            Meta typically reviews submissions in 1–24 hours.
          </p>
          <div className="mt-4">
            <Button asChild>
              <Link href="/templates/new">Submit your first template</Link>
            </Button>
          </div>
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border bg-card">
          <table className="w-full text-sm">
            <caption className="sr-only">Templates by status</caption>
            <thead className="border-b bg-muted/40 text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Name
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Category
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Language
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Status
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Last update
                </th>
              </tr>
            </thead>
            <tbody>
              {templates.map((t) => (
                <tr key={t.id} className="border-b last:border-b-0">
                  <td className="px-4 py-3">
                    <div className="font-medium">{t.name}</div>
                    {t.status === "rejected" && t.rejection_reason ? (
                      <div className="mt-0.5 text-xs text-destructive">
                        {t.rejection_reason}
                      </div>
                    ) : null}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {t.category}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">{t.language}</td>
                  <td className="px-4 py-3">
                    <Badge
                      variant="outline"
                      className={cn(
                        "border-0 font-normal capitalize",
                        STATUS_TONE[t.status],
                      )}
                    >
                      {t.status}
                    </Badge>
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {t.last_status_change_at
                      ? formatAbsolute(t.last_status_change_at)
                      : formatAbsolute(t.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
