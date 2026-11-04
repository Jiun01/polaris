/**
 * Dashboard widgets (Phase 8 PR-3).
 *
 * All five sub-components live in one file because none is reused
 * elsewhere yet — splitting prematurely would inflate the surface for
 * no review benefit. When PR-5 reuses QuotaDial in the Channels-tier
 * upsell or PR-10 lifts QuotaSparkline into Billing, extract then.
 *
 * Source data: the DashboardSummary envelope from
 * `polaris_shared_types.portal_dashboard`, now imported from
 * @polaris/shared-types (confirmed a true 1:1 SSOT match during
 * audit-batch-f-frontend-consolidation, 2026-07 — tenant_dashboard.py's
 * GET /api/tenant/dashboard/summary uses DashboardSummary directly as
 * response_model).
 */

import * as React from "react";
import Link from "next/link";
import {
  MessageCircle,
  Megaphone,
  Tag,
  Upload,
  type LucideIcon,
} from "lucide-react";
import type {
  DashboardSummary,
  DashboardQuotaSummary,
  DashboardRecentConversation,
} from "@polaris/shared-types";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import {
  formatCount,
  formatPercent,
  formatRelative,
  quotaDialColor,
} from "@/lib/format";
import { cn } from "@/lib/utils";

export type { DashboardQuotaSummary, DashboardRecentConversation };
// DashboardSummaryDTO kept as the local alias name (many call sites import
// it) — re-exported from the generated DashboardSummary rather than
// redeclared. plan_tier/suspension_state are `Field(...)`-defaulted in
// Pydantic so the generated type marks them optional (`?:`); the router
// always sets both explicitly, so treat as always-present at call sites.
export type DashboardSummaryDTO = DashboardSummary;

// --------------------------------------------------------------------
// Quota dial — bot_replies primary, knowledge bar secondary
// --------------------------------------------------------------------

export function QuotaDial({ quota }: { quota: DashboardQuotaSummary }) {
  const used = quota.bot_replies_used;
  const cap = quota.bot_replies_cap;
  const dial = quotaDialColor(used, cap);
  const pct = cap > 0 ? Math.min(100, (used / cap) * 100) : 0;
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription>Bot replies this period</CardDescription>
        <CardTitle className={cn("text-3xl tabular-nums", dial.text)}>
          {formatCount(used)}
          <span className="ml-2 text-base font-normal text-muted-foreground">
            / {formatCount(cap)}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div
          className="relative h-2 w-full overflow-hidden rounded-full bg-muted"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={cap}
          aria-valuenow={used}
          aria-label={`Bot replies used: ${formatPercent(used, cap)}`}
        >
          <div
            className={cn(
              "absolute inset-y-0 left-0 rounded-full transition-[width]",
              dial.band === "red"
                ? "bg-destructive"
                : dial.band === "amber"
                  ? "bg-warning"
                  : "bg-primary",
            )}
            style={{ width: `${pct}%` }}
          />
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          {formatPercent(used, cap)} used.{" "}
          {dial.band === "amber"
            ? "Approaching limit. Consider upgrading."
            : null}
          {dial.band === "red"
            ? "Limit reached. Bot replies have paused."
            : null}
        </p>
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------
// Recent conversations preview — 5 rows
// --------------------------------------------------------------------

// SSOT ConversationChannel (polaris_shared_types.conversation_view) is
// {whatsapp, messenger, demo, telegram, instagram_login} — legacy
// "instagram" is NOT in this union (recent_conversations is fed from a
// different, already-normalized query path than the conversations list
// page, which still needs the legacy value per Decision #2 of
// audit-batch-f-frontend-consolidation.plan.md's channel-vocab research).
const channelLabel: Record<DashboardRecentConversation["channel"], string> = {
  whatsapp: "WhatsApp",
  instagram_login: "Instagram",
  messenger: "Messenger",
  telegram: "Telegram",
  demo: "Demo",
};

// Map raw enum values to user-facing labels so the portal never exposes
// snake_case / lowercase tokens to tenant_admins.
const statusLabel: Record<DashboardRecentConversation["status"], string> = {
  open: "Open",
  resolved: "Resolved",
  pending: "Waiting",
  snoozed: "Snoozed",
};

export function RecentConversations({
  rows,
}: {
  rows: DashboardRecentConversation[];
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Recent conversations</CardTitle>
        <CardDescription>Last 5 inbound from any channel</CardDescription>
      </CardHeader>
      <CardContent className="pt-0">
        {rows.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            No conversations yet.{" "}
            <Link
              href="/channels"
              className="font-medium text-foreground underline underline-offset-2"
            >
              Connect a channel
            </Link>{" "}
            to start seeing inbound here.
          </p>
        ) : (
          <ul className="divide-y divide-border">
            {rows.map((c) => (
              <li key={c.conversation_id}>
                <Link
                  href={`/conversations/${c.conversation_id}`}
                  className="flex items-center justify-between gap-3 py-2.5 hover:bg-muted/50 cursor-pointer rounded-sm"
                >
                  <div className="flex min-w-0 flex-1 items-center gap-3">
                    <MessageCircle
                      className="h-4 w-4 shrink-0 text-muted-foreground"
                      aria-hidden
                    />
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">
                        {c.customer_name ?? "Unnamed contact"}
                      </p>
                      <p className="truncate text-xs text-muted-foreground">
                        {channelLabel[c.channel] ?? c.channel}
                        {c.intent ? ` · ${c.intent}` : ""}
                        {" · "}
                        {formatRelative(c.last_message_at)}
                      </p>
                    </div>
                  </div>
                  <Badge
                    variant={c.status === "open" ? "default" : "secondary"}
                    className="shrink-0"
                  >
                    {statusLabel[c.status]}
                  </Badge>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------
// Numeric tile (used for broadcasts / promotions / ingestion counts)
// --------------------------------------------------------------------

interface NumericTileProps {
  label: string;
  value: number;
  icon: LucideIcon;
  hint?: string;
}

export function NumericTile({
  label,
  value,
  icon: Icon,
  hint,
}: NumericTileProps) {
  return (
    <Card>
      <CardContent className="flex items-center gap-3 py-4">
        <div className="flex h-9 w-9 items-center justify-center rounded-full bg-muted">
          <Icon className="h-4 w-4 text-muted-foreground" aria-hidden />
        </div>
        <div className="min-w-0">
          <p className="text-xs text-muted-foreground">{label}</p>
          <p className="text-xl font-semibold tabular-nums">
            {formatCount(value)}
          </p>
          {hint ? (
            <p className="text-[11px] text-muted-foreground">{hint}</p>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}

export function ActiveBroadcastsCount({ value }: { value: number }) {
  return (
    <NumericTile
      label="Active broadcasts"
      value={value}
      icon={Megaphone}
      hint="Scheduled or in-flight"
    />
  );
}

export function ActivePromotionsCount({ value }: { value: number }) {
  return (
    <NumericTile
      label="Active promotions"
      value={value}
      icon={Tag}
      hint="Currently valid"
    />
  );
}

export function IngestionInFlight({ value }: { value: number }) {
  return (
    <NumericTile
      label="Ingestion in flight"
      value={value}
      icon={Upload}
      hint="Pending or running"
    />
  );
}

// --------------------------------------------------------------------
// Quota sparkline — pure inline SVG, no chart lib dep
// --------------------------------------------------------------------

export function QuotaSparkline({ values }: { values: number[] }) {
  const max = Math.max(1, ...values);
  const width = 280;
  const height = 48;
  const stepX = values.length > 1 ? width / (values.length - 1) : 0;
  const points = values
    .map((v, i) => {
      const x = i * stepX;
      const y = height - (v / max) * (height - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const total = values.reduce((a, b) => a + b, 0);

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Replies (last 7 days)</CardTitle>
        <CardDescription>{formatCount(total)} total</CardDescription>
      </CardHeader>
      <CardContent>
        <svg
          viewBox={`0 0 ${width} ${height}`}
          width="100%"
          height={height}
          role="img"
          aria-label={`Reply count for the last 7 days: ${values.join(", ")}`}
          className="overflow-visible"
        >
          <polyline
            fill="none"
            stroke="hsl(var(--primary))"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            points={points}
          />
        </svg>
        <Separator className="mt-2" />
        <div className="mt-2 flex justify-between text-[10px] text-muted-foreground">
          <span>7d ago</span>
          <span>Today</span>
        </div>
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------
// Suspension banner (renders only when suspension_state is set)
// --------------------------------------------------------------------

export function SuspensionBanner({
  state,
}: {
  state: "quota_exhausted" | "payment_overdue" | null;
}) {
  if (state === null) return null;
  if (state === "payment_overdue") {
    // payment_overdue is a hard lockout — destructive treatment.
    // Middleware should redirect to /pay before this renders, but
    // surfaced here as belt-and-braces.
    return (
      <div
        role="alert"
        className="mb-4 rounded-lg border border-destructive bg-destructive/10 px-4 py-3 text-sm text-destructive"
      >
        Your account is suspended for an unpaid invoice.{" "}
        <Link className="font-medium underline" href="/pay">
          Settle payment
        </Link>
        .
      </div>
    );
  }
  // quota_exhausted is recoverable (upgrade unblocks); warning treatment
  // so the eye doesn't conflate it with the locked-out variant above.
  return (
    <div
      role="alert"
      className="mb-4 rounded-lg border border-warning bg-warning/10 px-4 py-3 text-sm text-warning"
    >
      Bot quota exhausted for this period. Customer messages continue to reach
      your messaging inbox; the bot itself has paused.{" "}
      <Link className="font-medium underline" href="/billing/plan-change">
        Upgrade plan
      </Link>{" "}
      to resume.
    </div>
  );
}
