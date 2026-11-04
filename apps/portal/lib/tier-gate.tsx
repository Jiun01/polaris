/**
 * Tier-gate primitives (Phase 8 PR-3).
 *
 * `meetsTier(current, min)` — boolean plan-tier comparison.
 * `<UpgradeCard />` — the "this is a Pro feature" upsell card rendered in
 * place of a gated surface. Per the portal design rule we NEVER render
 * null for a locked feature — showing the upgrade card teaches the path.
 */

import Link from "next/link";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { cn } from "@/lib/utils";

export type PlanTier = "starter" | "pro" | "ultra" | "enterprise";

const TIER_ORDER: Record<PlanTier, number> = {
  starter: 0,
  pro: 1,
  ultra: 2,
  enterprise: 3,
};

export function meetsTier(
  current: PlanTier | undefined,
  min: PlanTier,
): boolean {
  if (current === undefined) return false;
  return TIER_ORDER[current] >= TIER_ORDER[min];
}

interface UpgradeCardProps {
  feature: string;
  description: string;
  minTier: PlanTier;
  className?: string;
}

export function UpgradeCard({
  feature,
  description,
  minTier,
  className,
}: UpgradeCardProps) {
  return (
    <Card className={cn("border-dashed", className)}>
      <CardHeader>
        <CardTitle>
          {feature} is a {minTier === "pro" ? "Pro" : minTier} feature
        </CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>
        <Button asChild variant="default">
          <Link href="/billing/plan-change">Upgrade plan</Link>
        </Button>
      </CardContent>
    </Card>
  );
}
