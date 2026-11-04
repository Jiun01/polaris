import Link from "next/link";
import { notFound } from "next/navigation";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import {
  OverrideForm,
  type QuotaValues,
} from "@/components/quotas/OverrideForm";

interface TenantQuotaRow extends QuotaValues {
  tenant_id: string;
  tenant_name: string;
  plan_tier: string | null;
}

interface UsageResponse {
  items: TenantQuotaRow[];
  total: number;
}

async function fetchTenantQuota(
  tenantId: string,
): Promise<TenantQuotaRow | null> {
  try {
    const res = await adminServerFetch<UsageResponse>(
      adminRoutes.quotas.usage(),
    );
    if (!res) return null;
    return res.items.find((r) => r.tenant_id === tenantId) ?? null;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

export default async function QuotaOverridePage({
  params,
}: {
  params: { tenant_id: string };
}) {
  const quota = await fetchTenantQuota(params.tenant_id);
  if (!quota) notFound();

  const isEnterprise = quota.plan_tier === "enterprise";
  const capIsUnset =
    quota.bot_replies === 0 && quota.bot_replies_period_cap === 0;

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <Link
        href="/quotas"
        className="text-xs text-muted-foreground hover:text-foreground hover:underline"
      >
        ← Back to Quotas
      </Link>

      <div className="flex items-center justify-between gap-4">
        <h1 className="text-xl font-semibold tracking-tight">
          Quota Override: {quota.tenant_name}
        </h1>
        <Badge variant="outline" className="capitalize text-xs">
          {quota.plan_tier ?? "unknown"}
        </Badge>
      </div>

      {/* Usage summary */}
      <Card className="divide-y divide-border text-sm">
        <div className="flex justify-between px-4 py-2.5">
          <span className="text-muted-foreground">
            Bot replies used this period
          </span>
          <span className="font-mono font-medium">
            {quota.usage_bot_replies_this_period.toLocaleString()}
            {quota.bot_replies_period_cap > 0 && (
              <span className="text-muted-foreground">
                {" "}
                / {quota.bot_replies_period_cap.toLocaleString()} (
                {quota.usage_pct.toFixed(1)}%)
              </span>
            )}
          </span>
        </div>
        <div className="flex justify-between px-4 py-2.5">
          <span className="text-muted-foreground">Total agent seats</span>
          <span className="font-mono font-medium">
            {(
              quota.agent_seats_included + quota.agent_seats_extra
            ).toLocaleString()}
            <span className="text-muted-foreground text-xs">
              {" "}
              ({quota.agent_seats_included} included + {quota.agent_seats_extra}{" "}
              extra)
            </span>
          </span>
        </div>
      </Card>

      {/* Enterprise cap-unset warning */}
      {isEnterprise && capIsUnset && (
        <div className="rounded-md border border-warning/50 bg-warning/10 px-4 py-3 text-sm text-warning">
          <strong>Enterprise cap not set.</strong> This tenant&apos;s bot-reply
          cap is 0, which means the bot will be blocked immediately. Set the
          contracted monthly cap in the <strong>Bot Replies</strong> tab below.
        </div>
      )}

      <OverrideForm tenantId={params.tenant_id} quota={quota} />
    </div>
  );
}
