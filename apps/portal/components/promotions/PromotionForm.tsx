"use client";

/**
 * Shared create/edit form for promotions (Phase 8 PR-9).
 *
 * Drives `POST /api/tenant/promotions/` on create and
 * `PATCH /api/tenant/promotions/{id}` on edit. Both endpoints take
 * the same TenantPromotionUpsert body shape; the form sends a full
 * snapshot on every save.
 *
 * Edit mode adds a Delete button. Routing back to /promotions after
 * any mutation refreshes the list via router.refresh().
 */

import * as React from "react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { apiFetch } from "@/lib/api/client";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";

interface PromotionDTO {
  id: string;
  tenant_branch_id: string | null;
  title: string;
  description: string;
  discount_text: string;
  terms: string | null;
  valid_from: string;
  valid_until: string;
  is_active: boolean;
}

interface BranchOption {
  id: string;
  name: string;
}

function defaultValidFrom(): string {
  return toLocalInput(new Date());
}

function defaultValidUntil(): string {
  const d = new Date();
  d.setDate(d.getDate() + 30);
  return toLocalInput(d);
}

function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

export function PromotionForm({
  mode,
  branches,
  initial,
}: {
  mode: "create" | "edit";
  branches: BranchOption[];
  initial?: PromotionDTO;
}) {
  const router = useRouter();
  const [title, setTitle] = React.useState(initial?.title ?? "");
  const [description, setDescription] = React.useState(
    initial?.description ?? "",
  );
  const [discountText, setDiscountText] = React.useState(
    initial?.discount_text ?? "",
  );
  const [terms, setTerms] = React.useState(initial?.terms ?? "");
  const [validFrom, setValidFrom] = React.useState(
    initial ? initial.valid_from.slice(0, 16) : defaultValidFrom(),
  );
  const [validUntil, setValidUntil] = React.useState(
    initial ? initial.valid_until.slice(0, 16) : defaultValidUntil(),
  );
  const [isActive, setIsActive] = React.useState(initial?.is_active ?? true);
  const [branchId, setBranchId] = React.useState(
    initial?.tenant_branch_id ?? "",
  );
  const [saving, setSaving] = React.useState(false);
  const [deleting, setDeleting] = React.useState(false);
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (new Date(validUntil) <= new Date(validFrom)) {
      setError('"Valid until" must be after "Valid from".');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const body = {
        title: title.trim(),
        description: description.trim(),
        discount_text: discountText.trim(),
        terms: terms.trim() || null,
        valid_from: new Date(validFrom).toISOString(),
        valid_until: new Date(validUntil).toISOString(),
        is_active: isActive,
        tenant_branch_id: branchId || null,
      };
      if (mode === "create") {
        await apiFetch(TenantRoutes.promotions(), {
          method: "POST",
          body: JSON.stringify(body),
        });
      } else if (initial) {
        await apiFetch(TenantRoutes.promotion(initial.id), {
          method: "PATCH",
          body: JSON.stringify(body),
        });
      }
      router.push("/promotions");
      router.refresh();
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't save. Try again.",
      );
    } finally {
      setSaving(false);
    }
  }

  async function executeDelete() {
    if (!initial) return;
    setConfirmOpen(false);
    setDeleting(true);
    setError(null);
    try {
      await apiFetch(TenantRoutes.promotion(initial.id), {
        method: "DELETE",
      });
      router.push("/promotions");
      router.refresh();
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't delete. Try again.",
      );
    } finally {
      setDeleting(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-5">
      <div className="space-y-2">
        <Label htmlFor="p-title">Title</Label>
        <Input
          id="p-title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          maxLength={200}
          required
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="p-discount">Discount text</Label>
        <Input
          id="p-discount"
          value={discountText}
          onChange={(e) => setDiscountText(e.target.value)}
          placeholder="20% off, RM 50 voucher, Buy 1 Free 1"
          maxLength={100}
          required
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="p-desc">Description</Label>
        <Textarea
          id="p-desc"
          rows={4}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          maxLength={2000}
          required
        />
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="p-from">Valid from</Label>
          <Input
            id="p-from"
            type="datetime-local"
            value={validFrom}
            onChange={(e) => setValidFrom(e.target.value)}
            required
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="p-until">Valid until</Label>
          <Input
            id="p-until"
            type="datetime-local"
            value={validUntil}
            onChange={(e) => setValidUntil(e.target.value)}
            required
          />
        </div>
      </div>

      <div className="space-y-2">
        <Label htmlFor="p-terms">Terms (optional)</Label>
        <Textarea
          id="p-terms"
          rows={3}
          value={terms}
          onChange={(e) => setTerms(e.target.value)}
          maxLength={4000}
        />
      </div>

      {branches.length > 0 ? (
        <div className="space-y-2">
          <Label htmlFor="p-branch">Branch (optional)</Label>
          <select
            id="p-branch"
            className="w-full max-w-xs rounded-lg bg-background px-2 py-1.5 text-sm outline outline-1 outline-input transition-colors hover:outline-n-slate-6"
            value={branchId}
            onChange={(e) => setBranchId(e.target.value)}
          >
            <option value="">All branches</option>
            {branches.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </select>
        </div>
      ) : null}

      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={isActive}
          onChange={(e) => setIsActive(e.target.checked)}
        />
        <span className="text-sm">Active (bot can surface this promotion)</span>
      </label>

      {error ? (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : null}

      <div className="flex items-center justify-between">
        <Button type="submit" disabled={saving}>
          {saving
            ? "Saving…"
            : mode === "create"
              ? "Create promotion"
              : "Save changes"}
        </Button>
        {mode === "edit" ? (
          <Button
            type="button"
            variant="outline"
            onClick={() => setConfirmOpen(true)}
            disabled={deleting}
            className="text-destructive hover:bg-destructive/10"
          >
            {deleting ? "Deleting…" : "Delete"}
          </Button>
        ) : null}
      </div>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete promotion?</DialogTitle>
            <DialogDescription>
              &ldquo;{initial?.title}&rdquo; will be permanently deleted and the
              bot will no longer surface it. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={executeDelete}
              disabled={deleting}
            >
              {deleting ? "Deleting…" : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </form>
  );
}
