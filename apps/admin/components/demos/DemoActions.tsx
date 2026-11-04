/**
 * Demo detail actions — ADR-066.
 *
 * Reset a visitor's one-demo-ever lock, and soft-delete the demo (frees its
 * code for reuse).
 */

"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { adminFetch } from "@/lib/api/client";
import { ApiError } from "@/lib/api/error";
import { adminRoutes } from "@/lib/api/routes";

export function DemoActions({
  demoId,
  demoName,
}: {
  demoId: string;
  demoName: string;
}) {
  const router = useRouter();
  const [visitor, setVisitor] = useState("");
  const [busy, setBusy] = useState(false);

  async function resetVisitor() {
    if (!visitor.trim()) return;
    setBusy(true);
    try {
      const res = await adminFetch<{ deleted: number }>(
        adminRoutes.demos.resetVisitor(demoId),
        { method: "POST", body: { channel_user_id: visitor.trim() } },
      );
      toast.success(
        `Reset ${res?.deleted ?? 0} session(s) for ${visitor.trim()}.`,
      );
      setVisitor("");
      router.refresh();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Reset failed.");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (
      !window.confirm(`Delete demo "${demoName}"? Its code is freed for reuse.`)
    ) {
      return;
    }
    setBusy(true);
    try {
      await adminFetch(adminRoutes.demos.remove(demoId), { method: "DELETE" });
      toast.success("Demo deleted.");
      router.push("/demo-system");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Delete failed.");
      setBusy(false);
    }
  }

  return (
    <Card className="space-y-4 border-destructive/20 p-4">
      <div>
        <p className="text-sm font-medium">Reset a visitor</p>
        <p className="text-xs text-muted-foreground">
          Clears a contact&apos;s one-demo-ever lock so they can demo again.
        </p>
        <div className="mt-2 flex gap-2">
          <Input
            value={visitor}
            onChange={(e) => setVisitor(e.target.value)}
            placeholder="+60123456789"
          />
          <Button
            variant="outline"
            onClick={resetVisitor}
            disabled={busy || !visitor.trim()}
          >
            Reset
          </Button>
        </div>
      </div>
      <div className="flex items-center justify-between border-t border-border pt-4">
        <p className="text-xs text-muted-foreground">
          Soft-deletes the demo + frees its code.
        </p>
        <Button variant="destructive" onClick={remove} disabled={busy}>
          Delete demo
        </Button>
      </div>
    </Card>
  );
}
