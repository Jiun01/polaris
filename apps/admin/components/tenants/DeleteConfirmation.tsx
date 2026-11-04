/**
 * Hard-delete confirmation dialog body (Phase 9 PR-3).
 *
 * Operator must type the tenant's slug to enable the destructive
 * button. Cascade scope is shown so the operator knows what's being
 * archived (invoices) vs deleted (everything else under the tenant).
 *
 * Submit posts to the DELETE endpoint via Server Action. On success →
 * toast + redirect to /tenants.
 */

"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";

interface CascadeScope {
  invoices_to_archive: number;
  audit_log_rows: number;
  channels: number;
  users: number;
  branches: number;
  promotions: number;
  audience_members: number;
}

export function DeleteConfirmation({
  tenantId,
  slug,
  displayName,
  cascade,
}: {
  tenantId: string;
  slug: string;
  displayName: string;
  cascade: CascadeScope;
}) {
  const router = useRouter();
  const [typed, setTyped] = useState("");
  const [pending, startTransition] = useTransition();

  const match = typed === slug;

  function submit() {
    if (!match) return;
    startTransition(async () => {
      try {
        await adminFetch<{ ok: true }>(
          adminRoutes.tenants.softDelete(tenantId),
          {
            method: "DELETE",
          },
        );
        toast.success(`Tenant ${slug} deleted. DSAR audit recorded.`);
        router.push("/tenants?status=active");
        router.refresh();
      } catch (err) {
        const msg =
          err instanceof ApiError
            ? `Deletion failed: ${err.message}`
            : "Deletion failed.";
        toast.error(msg);
      }
    });
  }

  return (
    <Card className="space-y-5 border-destructive/30 bg-destructive/5 p-6">
      <div>
        <h2 className="text-base font-semibold text-destructive">
          You are about to hard-delete {displayName}.
        </h2>
        <p className="mt-1 text-sm text-destructive/90">
          This is irreversible. An audit row is written, invoices are archived
          to long-term storage, and every row below is removed.
        </p>
      </div>

      <div className="rounded-md border border-destructive/30 bg-background p-3 text-sm">
        <p className="mb-2 text-xs uppercase tracking-wider text-muted-foreground">
          Cascade scope
        </p>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-1">
          <ScopeRow
            label="Invoices to archive"
            value={cascade.invoices_to_archive}
          />
          <ScopeRow label="Audit log rows" value={cascade.audit_log_rows} />
          <ScopeRow label="Channels" value={cascade.channels} />
          <ScopeRow label="Users" value={cascade.users} />
          <ScopeRow label="Branches" value={cascade.branches} />
          <ScopeRow label="Promotions" value={cascade.promotions} />
          <ScopeRow label="Audience members" value={cascade.audience_members} />
        </dl>
      </div>

      <div className="space-y-2">
        <Label htmlFor="confirm">
          Type the tenant name to confirm:{" "}
          <code className="rounded bg-muted px-1.5 py-0.5">{slug}</code>
        </Label>
        <Input
          id="confirm"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder={slug}
          autoComplete="off"
        />
      </div>

      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={() => router.back()}>
          Cancel
        </Button>
        <Button
          type="button"
          variant="destructive"
          disabled={!match || pending}
          onClick={submit}
        >
          {pending ? "Deleting…" : "Hard-delete tenant"}
        </Button>
      </div>
    </Card>
  );
}

function ScopeRow({ label, value }: { label: string; value: number }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right font-mono tabular-nums">{value}</dd>
    </>
  );
}
