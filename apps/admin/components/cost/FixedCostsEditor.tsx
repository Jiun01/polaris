"use client";

/**
 * Fixed-cost pool editor (cost-model rework 2026-06-01).
 *
 * Operator-entered platform fixed costs (Supabase, Redis, VPS, Grafana, Sentry,
 * domains, demo WhatsApp…). Nothing measures these — the operator maintains the
 * line items + monthly amounts here. After each mutation we router.refresh() so
 * the server-rendered Platform P&L card re-totals.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import type { PlatformFixedCost } from "@/lib/types/billing";

const inputCls =
  "h-8 rounded-md border border-input bg-transparent px-2 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

export function FixedCostsEditor({
  initial,
}: {
  initial: PlatformFixedCost[];
}) {
  const router = useRouter();
  const [items, setItems] = useState<PlatformFixedCost[]>(initial);
  const [busy, setBusy] = useState(false);
  const [newLabel, setNewLabel] = useState("");
  const [newCategory, setNewCategory] = useState("");
  const [newAmount, setNewAmount] = useState("0");

  async function patchItem(id: number, patch: Partial<PlatformFixedCost>) {
    setBusy(true);
    try {
      const updated = await adminFetch<PlatformFixedCost>(
        adminRoutes.cost.fixedItem(id),
        { method: "PATCH", body: patch },
      );
      if (updated) {
        setItems((prev) => prev.map((i) => (i.id === id ? updated : i)));
      }
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  async function removeItem(id: number) {
    setBusy(true);
    try {
      await adminFetch(adminRoutes.cost.fixedItem(id), { method: "DELETE" });
      setItems((prev) => prev.filter((i) => i.id !== id));
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  async function addItem() {
    if (!newLabel.trim()) return;
    setBusy(true);
    try {
      const created = await adminFetch<PlatformFixedCost>(
        adminRoutes.cost.fixed(),
        {
          method: "POST",
          body: {
            label: newLabel.trim(),
            category: newCategory.trim() || null,
            monthly_myr: newAmount || "0",
            active: true,
          },
        },
      );
      if (created) setItems((prev) => [...prev, created]);
      setNewLabel("");
      setNewCategory("");
      setNewAmount("0");
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="overflow-hidden">
      <table className="w-full text-sm">
        <thead className="bg-muted/50 text-left text-xs uppercase tracking-wider text-muted-foreground">
          <tr>
            <th className="px-4 py-2.5 font-medium">Line item</th>
            <th className="px-4 py-2.5 font-medium">Category</th>
            <th className="px-4 py-2.5 text-right font-medium">
              Monthly (MYR)
            </th>
            <th className="px-4 py-2.5 font-medium">Active</th>
            <th className="px-4 py-2.5 font-medium" />
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id} className="border-t border-border">
              <td className="px-4 py-2">{item.label}</td>
              <td className="px-4 py-2 text-muted-foreground">
                {item.category ?? "—"}
              </td>
              <td className="px-4 py-2 text-right">
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  // Render a CANONICAL numeric default (Number()) so the SSR
                  // markup matches what the browser's number input produces on
                  // hydration. Passing the raw API string ("0.00") makes the
                  // browser re-serialize the value and trips a React hydration
                  // mismatch (#418/#423/#425) — one per row — which discards the
                  // hydrated tree and breaks this editor's interactivity.
                  defaultValue={Number(item.monthly_myr)}
                  disabled={busy}
                  className={`${inputCls} w-28 text-right tabular-nums`}
                  onBlur={(e) => {
                    const v = e.target.value || "0";
                    if (Number(v) !== Number(item.monthly_myr)) {
                      void patchItem(item.id, { monthly_myr: v });
                    }
                  }}
                />
              </td>
              <td className="px-4 py-2">
                <input
                  type="checkbox"
                  checked={item.active}
                  disabled={busy}
                  onChange={(e) =>
                    void patchItem(item.id, { active: e.target.checked })
                  }
                  className="h-4 w-4"
                />
              </td>
              <td className="px-4 py-2 text-right">
                <button
                  onClick={() => void removeItem(item.id)}
                  disabled={busy}
                  className="text-xs text-destructive underline-offset-2 hover:underline disabled:opacity-50"
                >
                  Delete
                </button>
              </td>
            </tr>
          ))}
          {/* Add new */}
          <tr className="border-t border-border bg-muted/20">
            <td className="px-4 py-2">
              <input
                value={newLabel}
                onChange={(e) => setNewLabel(e.target.value)}
                placeholder="New line item"
                disabled={busy}
                className={`${inputCls} w-full`}
              />
            </td>
            <td className="px-4 py-2">
              <input
                value={newCategory}
                onChange={(e) => setNewCategory(e.target.value)}
                placeholder="category"
                disabled={busy}
                className={`${inputCls} w-full`}
              />
            </td>
            <td className="px-4 py-2 text-right">
              <input
                type="number"
                min="0"
                step="0.01"
                value={newAmount}
                onChange={(e) => setNewAmount(e.target.value)}
                disabled={busy}
                className={`${inputCls} w-28 text-right tabular-nums`}
              />
            </td>
            <td className="px-4 py-2" />
            <td className="px-4 py-2 text-right">
              <Button
                size="sm"
                onClick={() => void addItem()}
                disabled={busy || !newLabel.trim()}
              >
                Add
              </Button>
            </td>
          </tr>
        </tbody>
      </table>
    </Card>
  );
}
