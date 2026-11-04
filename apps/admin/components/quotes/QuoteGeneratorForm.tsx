"use client";

/**
 * Quote generator form (Phase 9 PR-9).
 *
 * Interactive form for /quotes/new. Shows a live proration preview as
 * the user selects tier + intended_signup_date. Mirrors the backend
 * proration formula: ratio = days_left / days_in_month.
 *
 * Tiers: Starter (1099, 5000), Pro (2499, 25000), Ultra (5499, 60000)
 *
 * Submit -> POST /api/admin/quotes -> shows download link via PdfPreview.
 */

import { useState, useMemo } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card } from "@/components/ui/card";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { formatMyr } from "@/lib/format";
import { PdfPreview } from "@/components/quotes/PdfPreview";
import type { SalesQuote } from "@/lib/types/billing";

type Tier = "starter" | "pro" | "ultra";
type BillingCycle = "monthly" | "annual";

interface TierConfig {
  label: string;
  monthly_price_myr: number;
  quota_replies: number;
}

const TIERS: Record<Tier, TierConfig> = {
  starter: { label: "Starter", monthly_price_myr: 1099, quota_replies: 5000 },
  pro: { label: "Pro", monthly_price_myr: 2499, quota_replies: 25000 },
  ultra: { label: "Ultra", monthly_price_myr: 5499, quota_replies: 60000 },
};

/** Days in a given month (1-indexed). */
function daysInMonth(year: number, month: number): number {
  return new Date(year, month, 0).getDate();
}

/** Days remaining in month from date (inclusive of date itself). */
function daysLeft(date: Date): number {
  const dim = daysInMonth(date.getFullYear(), date.getMonth() + 1);
  return dim - date.getDate() + 1;
}

interface ProrationPreview {
  tier: Tier;
  cycle: BillingCycle;
  headlinePrice: number;
  headlineQuota: number;
  proratedPrice: number;
  proratedQuota: number;
  daysRemaining: number;
  totalDays: number;
  monthName: string;
}
function computePreview(
  tier: Tier,
  cycle: BillingCycle,
  signupDateStr: string,
): ProrationPreview | null {
  const date = new Date(signupDateStr + "T00:00:00");
  if (isNaN(date.getTime())) return null;
  const cfg = TIERS[tier];
  const annualMultiplier = cycle === "annual" ? 10 : 1; // 10 months = 2 free
  const headlinePrice =
    cfg.monthly_price_myr * (cycle === "annual" ? annualMultiplier : 1);
  const headlineQuota = cfg.quota_replies;
  const dim = daysInMonth(date.getFullYear(), date.getMonth() + 1);
  const dl = daysLeft(date);
  const ratio = dl / dim;
  const proratedPrice = Math.round(cfg.monthly_price_myr * ratio * 100) / 100;
  const proratedQuota = Math.round(cfg.quota_replies * ratio);
  const monthName = date.toLocaleString("en-MY", {
    month: "long",
    year: "numeric",
  });
  return {
    tier,
    cycle,
    headlinePrice,
    headlineQuota,
    proratedPrice,
    proratedQuota,
    daysRemaining: dl,
    totalDays: dim,
    monthName,
  };
}

function ProrationPreviewCard({ preview }: { preview: ProrationPreview }) {
  const tierCfg = TIERS[preview.tier];
  return (
    <Card className="space-y-3 p-4 bg-muted/30">
      <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Live preview
      </p>
      <div className="space-y-1">
        <p className="text-sm font-medium">
          Headline: {tierCfg.label} at {formatMyr(tierCfg.monthly_price_myr)}/mo
          + {preview.headlineQuota.toLocaleString("en-MY")} replies
        </p>
        <p className="text-sm text-muted-foreground">
          First invoice: {formatMyr(preview.proratedPrice)} +{" "}
          {preview.proratedQuota.toLocaleString("en-MY")} replies for{" "}
          {preview.daysRemaining} days remaining in {preview.monthName}
        </p>
        <p className="text-xs text-muted-foreground">
          Ratio: {preview.daysRemaining}/{preview.totalDays} ={" "}
          {((preview.daysRemaining / preview.totalDays) * 100).toFixed(1)}%
        </p>
      </div>
    </Card>
  );
}
export function QuoteGeneratorForm() {
  const [tier, setTier] = useState<Tier>("pro");
  const [cycle, setCycle] = useState<BillingCycle>("monthly");
  const [signupDate, setSignupDate] = useState("");
  const [email, setEmail] = useState("");
  const [company, setCompany] = useState("");
  const [isPending, setIsPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SalesQuote | null>(null);

  const preview = useMemo(
    () => (signupDate ? computePreview(tier, cycle, signupDate) : null),
    [tier, cycle, signupDate],
  );

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setIsPending(true);
    setError(null);
    try {
      const res = await adminFetch<SalesQuote>(adminRoutes.quotes.create(), {
        method: "POST",
        body: {
          prospect_email: email || null,
          prospect_company: company || null,
          target_tier: tier,
          target_billing_cycle: cycle,
          intended_signup_date: signupDate,
        },
      });
      setResult(res ?? null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unexpected error");
    } finally {
      setIsPending(false);
    }
  }

  if (result) {
    return (
      <div className="space-y-4">
        <Card className="border-success/30 bg-success/5 p-4 text-sm text-success font-medium">
          Quote {result.quote_code} generated successfully.
        </Card>
        <PdfPreview quote={result} />
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-5">
      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-1.5">
          <Label htmlFor="prospect-email">
            Prospect email{" "}
            <span className="text-xs text-muted-foreground">(optional)</span>
          </Label>
          <Input
            id="prospect-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="prospect@company.com"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="prospect-company">
            Company{" "}
            <span className="text-xs text-muted-foreground">(optional)</span>
          </Label>
          <Input
            id="prospect-company"
            type="text"
            value={company}
            onChange={(e) => setCompany(e.target.value)}
            placeholder="Acme Sdn Bhd"
          />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-1.5">
          <Label htmlFor="target-tier">Target tier</Label>
          <select
            id="target-tier"
            value={tier}
            onChange={(e) => setTier(e.target.value as Tier)}
            className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            <option value="starter">Starter — RM 1,099/mo</option>
            <option value="pro">Pro — RM 2,499/mo</option>
            <option value="ultra">Ultra — RM 5,499/mo</option>
          </select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="billing-cycle">Billing cycle</Label>
          <select
            id="billing-cycle"
            value={cycle}
            onChange={(e) => setCycle(e.target.value as BillingCycle)}
            className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            <option value="monthly">Monthly</option>
            <option value="annual">Annual (2 months free)</option>
          </select>
        </div>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="signup-date">Intended signup date</Label>
        <Input
          id="signup-date"
          type="date"
          required
          value={signupDate}
          onChange={(e) => setSignupDate(e.target.value)}
        />
      </div>
      {preview && <ProrationPreviewCard preview={preview} />}
      {error && <p className="text-sm text-destructive">{error}</p>}
      <Button type="submit" disabled={isPending}>
        {isPending ? "Generating…" : "Generate quote"}
      </Button>
    </form>
  );
}
