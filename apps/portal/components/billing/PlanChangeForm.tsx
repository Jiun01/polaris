"use client";

/**
 * Plan-change request form (Phase 8 PR-10).
 *
 * Three tiles side-by-side (Starter / Pro / Ultra) — Enterprise is
 * sales-led so we link out instead of offering it as a target. Tenant
 * picks a target tier + bills cycle (annual cycle is GA-deferred; we
 * surface the field for future use but lock it to monthly).
 *
 * Submission POSTs to /api/tenant/plan-change-requests/ which computes
 * the proration server-side. Server returns the full pending request
 * including the proration snapshot.
 */

import * as React from "react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useTenantContext } from "@/components/providers/tenant-provider";
import { apiFetch } from "@/lib/api/client";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { formatMyr } from "@/lib/format";
import { cn } from "@/lib/utils";

type Tier = "starter" | "pro" | "ultra";

const TIER_LABEL: Record<Tier, string> = {
  starter: "Starter",
  pro: "Pro",
  ultra: "Ultra",
};

// Monthly headline price (MYR). Matches proration._TIER_PRICE; annual bills
// at ×12×0.85 (≈ RM 699 / 999 / 2,599 effective/mo).
const TIER_PRICE: Record<Tier, number> = {
  starter: 822,
  pro: 1176,
  ultra: 3058,
};

// Reply quotas + entitlements mirror admin_tenants._TIER_QUOTA_DEFAULTS.
const TIER_HIGHLIGHTS: Record<Tier, string[]> = {
  starter: ["WhatsApp inbox", "100 MB knowledge", "1,500 bot replies/mo"],
  pro: [
    "+ IG, Messenger, Telegram, Email",
    "500 MB knowledge",
    "5,000 bot replies/mo",
    "Broadcasts",
  ],
  ultra: [
    "Everything in Pro",
    "2 GB knowledge",
    "15,000 bot replies/mo",
    "Branches",
    "Priority support",
  ],
};

interface PlanChangeResponse {
  id: string;
  status: string;
  proration_preview: {
    amount_due_myr: number;
    period_credited_myr: number;
    quota_topup_bot_replies: number;
  };
}

function EnterpriseConcierge() {
  return (
    <div className="rounded-xl border bg-card p-6 space-y-3">
      <h2 className="text-base font-semibold">Enterprise plan</h2>
      <p className="text-sm text-muted-foreground">
        Your Enterprise plan is managed by our team. Email{" "}
        <a
          className="underline"
          href="mailto:sales@getpolaris.my?subject=Enterprise%20plan%20change"
        >
          sales@getpolaris.my
        </a>{" "}
        to change it.
      </p>
    </div>
  );
}

export function PlanChangeForm({ initialTarget }: { initialTarget: string }) {
  const router = useRouter();
  const { plan_tier } = useTenantContext();

  const validTargets = ["starter", "pro", "ultra"] as Tier[];
  const defaultTarget = (validTargets as string[]).includes(initialTarget)
    ? (initialTarget as Tier)
    : "pro";
  const [target, setTarget] = React.useState<Tier>(defaultTarget);
  const [reason, setReason] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [preview, setPreview] = React.useState<PlanChangeResponse | null>(null);

  // Enterprise tenants cannot self-serve — show concierge message instead.
  // All hooks are called above this guard to satisfy Rules of Hooks.
  if (plan_tier === "enterprise") {
    return <EnterpriseConcierge />;
  }

  const currentTier = plan_tier as Tier;
  const sameAsCurrent = target === currentTier;

  async function submit() {
    if (sameAsCurrent) {
      setError("Pick a different tier than your current plan.");
      return;
    }
    setSubmitting(true);
    setError(null);
    setPreview(null);
    try {
      const res = await apiFetch<PlanChangeResponse>(
        TenantRoutes.planChangeRequests(),
        {
          method: "POST",
          body: JSON.stringify({
            target_tier: target,
            target_billing_cycle: "monthly",
            reason: reason.trim() || null,
          }),
        },
      );
      if (res) setPreview(res);
      router.refresh();
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't submit the request. Try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="grid gap-3 sm:grid-cols-3">
        {validTargets.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTarget(t)}
            aria-pressed={target === t}
            className={cn(
              "rounded-xl border bg-card p-4 text-left transition-colors",
              target === t
                ? "border-foreground ring-2 ring-foreground/20"
                : "border-border hover:border-foreground/40",
              currentTier === t && "opacity-70",
            )}
          >
            <div className="flex items-baseline justify-between">
              <h3 className="font-semibold">{TIER_LABEL[t]}</h3>
              {currentTier === t ? (
                <span className="text-xs text-muted-foreground">Current</span>
              ) : null}
            </div>
            <div className="mt-1 text-lg font-semibold">
              {formatMyr(TIER_PRICE[t])}
              <span className="text-sm font-normal text-muted-foreground">
                {" "}
                / month
              </span>
            </div>
            <ul className="mt-3 space-y-1 text-xs text-muted-foreground">
              {TIER_HIGHLIGHTS[t].map((h) => (
                <li key={h}>• {h}</li>
              ))}
            </ul>
          </button>
        ))}
      </div>

      <div className="space-y-2">
        <label htmlFor="reason" className="text-sm font-medium">
          Why are you changing plans? (optional)
        </label>
        <Textarea
          id="reason"
          rows={3}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          maxLength={1000}
          placeholder="Helpful context for our team when reviewing your request."
        />
      </div>

      {error ? (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : null}

      {preview ? (
        <div
          role="status"
          className="rounded-lg border border-emerald-300 bg-emerald-50 px-4 py-3 text-sm dark:border-emerald-900 dark:bg-emerald-950"
        >
          <strong>Request submitted.</strong> Proration estimate:{" "}
          {formatMyr(preview.proration_preview.amount_due_myr)} due now (credit{" "}
          {formatMyr(preview.proration_preview.period_credited_myr)} for unused
          time). We&apos;ll send a notification once admin approves.
        </div>
      ) : null}

      <div className="flex items-center gap-3">
        <Button
          onClick={submit}
          disabled={submitting || sameAsCurrent || preview !== null}
        >
          {submitting
            ? "Submitting…"
            : preview
              ? "Submitted"
              : `Request ${TIER_LABEL[target]} plan`}
        </Button>
        <p className="text-xs text-muted-foreground">
          Enterprise plan? Contact{" "}
          <a
            className="underline"
            href="mailto:sales@getpolaris.my?subject=Enterprise%20plan%20inquiry"
          >
            sales@getpolaris.my
          </a>
          .
        </p>
      </div>
    </div>
  );
}
