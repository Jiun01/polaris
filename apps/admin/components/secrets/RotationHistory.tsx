/**
 * Rotation history table (Phase 9 PR-4).
 *
 * Reads audit_logs rows where action=secret.rotated for the given
 * tenant. Rendered from a pre-fetched list passed as a prop (server
 * component caller fetches via adminServerFetch).
 *
 * Shows: service, rotated_at (MYT), actor.
 */

import { Card } from "@/components/ui/card";
import { formatMyt } from "@/lib/format";

export interface RotationEntry {
  id: string;
  occurred_at: string;
  service: string;
  actor_label: string;
}

export function RotationHistory({ entries }: { entries: RotationEntry[] }) {
  if (entries.length === 0) {
    return (
      <Card className="border-dashed p-4 text-sm text-muted-foreground">
        No rotation history yet for this tenant.
      </Card>
    );
  }

  return (
    <Card className="overflow-hidden">
      <h2 className="px-4 py-3 text-sm font-semibold border-b border-border">
        Rotation history
      </h2>
      <table className="w-full text-sm">
        <thead className="bg-muted/50 text-left text-xs uppercase tracking-wider text-muted-foreground">
          <tr>
            <th scope="col" className="px-4 py-2 font-medium">
              Service
            </th>
            <th scope="col" className="px-4 py-2 font-medium">
              Rotated at
            </th>
            <th scope="col" className="px-4 py-2 font-medium">
              Actor
            </th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr
              key={entry.id}
              className="border-t border-border hover:bg-muted/40 transition"
            >
              <td className="px-4 py-2 font-mono text-xs">{entry.service}</td>
              <td className="px-4 py-2 text-muted-foreground">
                {formatMyt(entry.occurred_at)}
              </td>
              <td className="px-4 py-2 text-xs text-muted-foreground">
                {entry.actor_label}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}
