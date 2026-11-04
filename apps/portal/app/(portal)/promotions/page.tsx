/**
 * Promotions list — Screen #11 (Phase 8 PR-9).
 *
 * Universal across tiers per ADR-042 — every plan tier sees this
 * screen identically. CRUD against /api/tenant/promotions.
 */

import Link from "next/link";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatAbsolute } from "@/lib/format";

interface PromotionDTO {
  id: string;
  tenant_id: string;
  tenant_branch_id: string | null;
  title: string;
  description: string;
  discount_text: string;
  terms: string | null;
  valid_from: string;
  valid_until: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

async function loadPromotions(): Promise<{
  items: PromotionDTO[];
  error?: string;
}> {
  try {
    const res = await apiFetchServer<PromotionDTO[]>(TenantRoutes.promotions());
    return { items: res ?? [] };
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error("[portal /promotions] fetch failed", err);
    return {
      items: [],
      error:
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't load promotions.",
    };
  }
}

function isLive(p: PromotionDTO, nowIso: string): boolean {
  return p.is_active && p.valid_from <= nowIso && p.valid_until >= nowIso;
}

export default async function PromotionsPage() {
  const { items, error } = await loadPromotions();
  const nowIso = new Date().toISOString();

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Promotions</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Discounts, deals, and campaign offers the bot can pitch in response
            to relevant customer questions.
          </p>
        </div>
        <Button asChild>
          <Link href="/promotions/new">New promotion</Link>
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

      {items.length === 0 && !error ? (
        <div className="rounded-xl border border-dashed bg-muted/30 p-8 text-center">
          <h2 className="text-base font-semibold">No promotions yet</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Create a promotion and the bot will surface it when a customer asks
            about pricing or offers.
          </p>
          <div className="mt-4">
            <Button asChild>
              <Link href="/promotions/new">Create your first promotion</Link>
            </Button>
          </div>
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {items.map((p) => (
            <Link
              key={p.id}
              href={`/promotions/${p.id}`}
              className="rounded-xl border bg-card p-4 transition-colors hover:border-foreground/40"
            >
              <div className="flex items-start justify-between gap-2">
                <h3 className="font-semibold">{p.title}</h3>
                {isLive(p, nowIso) ? (
                  <Badge
                    variant="outline"
                    className="border-0 bg-emerald-100 text-xs font-normal text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100"
                  >
                    Live
                  </Badge>
                ) : !p.is_active ? (
                  <Badge variant="outline" className="text-xs font-normal">
                    Paused
                  </Badge>
                ) : (
                  <Badge variant="outline" className="text-xs font-normal">
                    Scheduled
                  </Badge>
                )}
              </div>
              <p className="mt-2 line-clamp-2 text-sm text-muted-foreground">
                {p.description}
              </p>
              <p className="mt-2 text-sm font-medium text-foreground">
                {p.discount_text}
              </p>
              <p className="mt-3 text-xs text-muted-foreground">
                Valid {formatAbsolute(p.valid_from)} →{" "}
                {formatAbsolute(p.valid_until)}
              </p>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
