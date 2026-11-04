"use client";

/**
 * Shared create/edit form for products (Phase 15 PR-7).
 *
 * Drives `POST /api/tenant/products/` on create and
 * `PATCH /api/tenant/products/{id}` on edit (same TenantProductUpsert body).
 * On create we redirect to the edit page so the tenant can immediately add
 * photos. Edit mode adds the image gallery (multipart upload + delete) and a
 * delete-product button.
 *
 * Mirrors components/promotions/PromotionForm.tsx.
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

export interface ProductImageDTO {
  id: string;
  product_id: string;
  storage_path: string;
  sort_order: number;
  url: string | null;
}

export interface ProductDTO {
  id: string;
  tenant_branch_id: string | null;
  sku: string | null;
  name: string;
  description: string;
  price_text: string | null;
  is_active: boolean;
  images: ProductImageDTO[];
}

interface BranchOption {
  id: string;
  name: string;
}

export function ProductForm({
  mode,
  branches,
  initial,
}: {
  mode: "create" | "edit";
  branches: BranchOption[];
  initial?: ProductDTO;
}) {
  const router = useRouter();
  const [name, setName] = React.useState(initial?.name ?? "");
  const [sku, setSku] = React.useState(initial?.sku ?? "");
  const [priceText, setPriceText] = React.useState(initial?.price_text ?? "");
  const [description, setDescription] = React.useState(
    initial?.description ?? "",
  );
  const [isActive, setIsActive] = React.useState(initial?.is_active ?? true);
  const [branchId, setBranchId] = React.useState(
    initial?.tenant_branch_id ?? "",
  );
  const [images, setImages] = React.useState<ProductImageDTO[]>(
    initial?.images ?? [],
  );
  const [saving, setSaving] = React.useState(false);
  const [deleting, setDeleting] = React.useState(false);
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [uploading, setUploading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const body = {
        name: name.trim(),
        description: description.trim(),
        sku: sku.trim() || null,
        price_text: priceText.trim() || null,
        is_active: isActive,
        tenant_branch_id: branchId || null,
      };
      if (mode === "create") {
        const created = await apiFetch<ProductDTO>(TenantRoutes.products(), {
          method: "POST",
          body: JSON.stringify(body),
        });
        // Redirect to edit so the tenant can add photos straight away.
        router.push(created ? `/products/${created.id}` : "/products");
        router.refresh();
        return;
      }
      if (initial) {
        await apiFetch(TenantRoutes.product(initial.id), {
          method: "PATCH",
          body: JSON.stringify(body),
        });
        router.push("/products");
        router.refresh();
      }
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
      await apiFetch(TenantRoutes.product(initial.id), {
        method: "DELETE",
      });
      router.push("/products");
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

  async function onPickFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-picking the same file
    if (!file || !initial) return;
    setUploading(true);
    setError(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const created = await apiFetch<ProductImageDTO>(
        TenantRoutes.productImages(initial.id),
        { method: "POST", body: fd },
      );
      if (created) setImages((prev) => [...prev, created]);
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't upload image. Try a JPEG, PNG, or WebP under 15 MB.",
      );
    } finally {
      setUploading(false);
    }
  }

  async function deleteImage(imageId: string) {
    if (!initial) return;
    try {
      await apiFetch(TenantRoutes.productImage(initial.id, imageId), {
        method: "DELETE",
      });
      setImages((prev) => prev.filter((i) => i.id !== imageId));
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't remove image. Try again.",
      );
    }
  }

  return (
    <form onSubmit={submit} className="space-y-5">
      <div className="space-y-2">
        <Label htmlFor="p-name">Name</Label>
        <Input
          id="p-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={200}
          required
        />
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="p-sku">SKU (optional)</Label>
          <Input
            id="p-sku"
            value={sku}
            onChange={(e) => setSku(e.target.value)}
            placeholder="TUMBLER-BLUE-500"
            maxLength={64}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="p-price">Price (optional)</Label>
          <Input
            id="p-price"
            value={priceText}
            onChange={(e) => setPriceText(e.target.value)}
            placeholder="RM 49.90, from RM 20"
            maxLength={100}
          />
        </div>
      </div>

      <div className="space-y-2">
        <Label htmlFor="p-desc">Description</Label>
        <Textarea
          id="p-desc"
          rows={4}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          maxLength={4000}
          required
        />
        <p className="text-xs text-muted-foreground">
          The bot answers product questions from this text and sends the photo
          when a customer wants to see it.
        </p>
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
        <span className="text-sm">Active (bot can surface this product)</span>
      </label>

      {mode === "edit" ? (
        <div className="space-y-3 rounded-xl border bg-card p-4">
          <div className="flex items-center justify-between">
            <Label>Photos</Label>
            <label className="cursor-pointer text-sm text-primary hover:underline">
              {uploading ? "Uploading…" : "Add photo"}
              <input
                type="file"
                accept="image/jpeg,image/png,image/webp"
                className="sr-only"
                onChange={onPickFile}
                disabled={uploading}
              />
            </label>
          </div>
          {images.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No photos yet. Add one and the bot can send it to customers.
            </p>
          ) : (
            <div className="grid grid-cols-3 gap-3 sm:grid-cols-4">
              {images.map((img) => (
                <div
                  key={img.id}
                  className="group relative aspect-square overflow-hidden rounded-lg border bg-muted"
                >
                  {img.url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={img.url}
                      alt={name || "Product photo"}
                      className="h-full w-full object-cover"
                    />
                  ) : (
                    <div className="flex h-full w-full items-center justify-center text-xs text-muted-foreground">
                      Image
                    </div>
                  )}
                  <button
                    type="button"
                    onClick={() => deleteImage(img.id)}
                    className="absolute right-1 top-1 rounded bg-black/60 px-1.5 py-0.5 text-xs text-white opacity-0 transition-opacity group-hover:opacity-100"
                    aria-label="Remove photo"
                  >
                    Remove
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">
          Save the product first, then add photos on the next screen.
        </p>
      )}

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
              ? "Create product"
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
            <DialogTitle>Delete product?</DialogTitle>
            <DialogDescription>
              &ldquo;{initial?.name}&rdquo; and all its photos will be
              permanently deleted. This cannot be undone.
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
