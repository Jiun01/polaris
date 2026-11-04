/**
 * Demo System list table — ADR-066.
 *
 * Dense, operator-grade list of demo brands: code, name, caps, active
 * session count. Mirrors components/tenants/TenantsTable.tsx.
 */

import Link from "next/link";
import { ChevronRight } from "lucide-react";

import { Card } from "@/components/ui/card";
import { formatMyt, formatInt } from "@/lib/format";
import type { DemoSummary } from "@/components/demos/types";

export function DemosTable({ items }: { items: DemoSummary[] }) {
  if (items.length === 0) {
    return (
      <Card className="flex flex-col items-center justify-center gap-2 border-dashed py-12 text-center">
        <p className="text-sm font-medium">No demos yet.</p>
        <p className="text-sm text-muted-foreground">
          Create one to hand a prospect a demo code for your live channels.
        </p>
        <Link
          href="/demo-system/new"
          className="mt-2 text-sm font-medium text-primary underline-offset-4 hover:underline"
        >
          New demo →
        </Link>
      </Card>
    );
  }

  return (
    <Card className="overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-left text-xs uppercase tracking-wider text-muted-foreground">
            <tr>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Demo code
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Brand
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Duration (h)
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Msg cap
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Active
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Created
              </th>
              <th scope="col" className="px-4 py-2.5"></th>
            </tr>
          </thead>
          <tbody>
            {items.map((d) => (
              <tr
                key={d.id}
                className="border-t border-border transition hover:bg-muted/40"
              >
                <td className="px-4 py-2.5 font-mono text-xs">
                  {d.demo_code ?? "—"}
                </td>
                <td className="px-4 py-2.5">
                  <div className="flex flex-col">
                    <Link
                      href={`/demo-system/${d.id}`}
                      className="font-medium text-foreground hover:underline"
                    >
                      {d.display_name}
                    </Link>
                    <span className="text-xs text-muted-foreground">
                      {d.slug}
                    </span>
                  </div>
                </td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums">
                  {formatInt(d.duration_hours)}
                </td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums">
                  {formatInt(d.max_messages)}
                </td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums">
                  {formatInt(d.active_sessions)}
                </td>
                <td className="px-4 py-2.5 text-muted-foreground">
                  {formatMyt(d.created_at)}
                </td>
                <td className="px-4 py-2.5 text-right">
                  <Link
                    href={`/demo-system/${d.id}`}
                    aria-label={`Open ${d.display_name}`}
                    className="inline-flex items-center text-muted-foreground hover:text-foreground"
                  >
                    <ChevronRight className="h-4 w-4" aria-hidden />
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
