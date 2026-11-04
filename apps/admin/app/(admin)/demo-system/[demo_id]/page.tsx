/**
 * Demo detail — ADR-066.
 *
 * Demo config + sessions/leads log + a link into the full tenant view
 * (where KB/products are managed via impersonation, reusing the existing
 * flow — demo brands are real tenant rows).
 */

import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DemoActions } from "@/components/demos/DemoActions";
import { DemoSessionsTable } from "@/components/demos/DemoSessionsTable";
import type { DemoSessionRow, DemoSummary } from "@/components/demos/types";
import { ApiError } from "@/lib/api/error";
import { adminRoutes } from "@/lib/api/routes";
import { adminServerFetch } from "@/lib/api/server";

async function load(
  demoId: string,
): Promise<{ demo: DemoSummary; sessions: DemoSessionRow[] } | null> {
  try {
    const demo = await adminServerFetch<DemoSummary>(
      adminRoutes.demos.detail(demoId),
    );
    if (!demo) return null;
    let sessions: DemoSessionRow[] = [];
    try {
      sessions =
        (await adminServerFetch<DemoSessionRow[]>(
          adminRoutes.demos.sessions(demoId),
        )) ?? [];
    } catch {
      // sessions are best-effort on the detail view
    }
    return { demo, sessions };
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </p>
      <p className="mt-0.5 font-medium">{value}</p>
    </div>
  );
}

export default async function DemoDetailPage({
  params,
}: {
  params: { demo_id: string };
}) {
  const data = await load(params.demo_id);
  if (!data) notFound();
  const { demo, sessions } = data;

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <Link
        href="/demo-system"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" aria-hidden />
        Demo System
      </Link>

      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">
            {demo.display_name}
          </h1>
          <p className="mt-1 font-mono text-sm text-muted-foreground">
            {demo.demo_code ?? "—"}
          </p>
        </div>
        <Button asChild variant="outline">
          <Link href={`/tenants/${demo.id}`}>
            Manage in tenant view (KB / impersonate) →
          </Link>
        </Button>
      </div>

      <Card className="grid grid-cols-3 gap-4 p-4">
        <Stat label="Duration" value={`${demo.duration_hours} h`} />
        <Stat label="Message cap" value={String(demo.max_messages)} />
        <Stat label="Active sessions" value={String(demo.active_sessions)} />
      </Card>

      <div>
        <h2 className="mb-2 text-sm font-semibold">Sessions &amp; leads</h2>
        <DemoSessionsTable items={sessions} />
      </div>

      <DemoActions demoId={demo.id} demoName={demo.display_name} />
    </div>
  );
}
