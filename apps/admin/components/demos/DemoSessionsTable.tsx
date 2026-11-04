/**
 * Demo sessions / leads table — ADR-066.
 *
 * Read-only log of demo sessions for one brand: who, how many messages,
 * expired?, and whether the lead was flagged on demo end.
 */

import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { formatMyt } from "@/lib/format";
import type { DemoSessionRow } from "@/components/demos/types";

export function DemoSessionsTable({ items }: { items: DemoSessionRow[] }) {
  if (items.length === 0) {
    return (
      <Card className="border-dashed py-8 text-center text-sm text-muted-foreground">
        No sessions yet.
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
                Contact
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Messages
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                State
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Lead
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Started
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((s) => (
              <tr
                key={`${s.channel_user_id}-${s.activated_at}`}
                className="border-t border-border"
              >
                <td className="px-4 py-2.5 font-mono text-xs">
                  {s.channel_user_id}
                  <span className="ml-1 text-muted-foreground">
                    ({s.channel})
                  </span>
                </td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums">
                  {s.message_count}/{s.max_messages}
                </td>
                <td className="px-4 py-2.5">
                  <Badge variant="outline" className="capitalize">
                    {s.is_expired ? "ended" : "active"}
                  </Badge>
                </td>
                <td className="px-4 py-2.5">
                  {s.lead_flagged ? (
                    <Badge
                      variant="outline"
                      className="border-success/30 bg-success/15 text-success"
                    >
                      flagged
                    </Badge>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </td>
                <td className="px-4 py-2.5 text-muted-foreground">
                  {formatMyt(s.activated_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
