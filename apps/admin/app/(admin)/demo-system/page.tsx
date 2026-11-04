/**
 * Demo System list — ADR-066.
 *
 * Owner-only management of demo brands. Each demo is a hidden tenant the
 * operator owns; a prospect types its code on the live channels to roleplay
 * that brand's assistant (see services/demo_runtime.py).
 */

import Link from "next/link";
import { Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DemosTable } from "@/components/demos/DemosTable";
import type { DemoSummary } from "@/components/demos/types";
import { ApiError } from "@/lib/api/error";
import { adminRoutes } from "@/lib/api/routes";
import { adminServerFetch } from "@/lib/api/server";

async function fetchDemos(): Promise<DemoSummary[] | { error: string }> {
  try {
    const res = await adminServerFetch<DemoSummary[]>(adminRoutes.demos.list());
    return res ?? [];
  } catch (err) {
    if (err instanceof ApiError) {
      if (err.status === 404 || err.status === 501) return [];
      return { error: err.message };
    }
    throw err;
  }
}

export default async function DemoSystemPage() {
  const data = await fetchDemos();

  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Demo System</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Demo brands a prospect can trigger by typing a code on your live
            channels. Excluded from billing + metrics.
          </p>
        </div>
        <Button asChild>
          <Link
            href="/demo-system/new"
            className="inline-flex items-center gap-1.5"
          >
            <Plus className="h-4 w-4" aria-hidden />
            New demo
          </Link>
        </Button>
      </div>

      {"error" in data ? (
        <Card className="border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          Failed to load demos: {data.error}
        </Card>
      ) : (
        <DemosTable items={data} />
      )}
    </div>
  );
}
