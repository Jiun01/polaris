"use client";

/**
 * Pro+ Branches CRUD client (Phase 8 PR-9).
 *
 * Inline create form + per-row rename + per-row delete. No detail
 * page — branches at GA carry only a name field, so the list view
 * doubles as the edit surface.
 *
 * Chatwoot agent invite per checklist task: deferred to v2.1 — the
 * portal_design_prompt §15 calls for combining branch create with a
 * Chatwoot inbox invite, but that requires Chatwoot API plumbing
 * (server-side) which is out of scope for PR-9. Branches as a
 * routing tag ships now; agent-invite glue lands later.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { MapPin, Pencil, Plus, Trash2 } from "lucide-react";

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
import { apiFetch } from "@/lib/api/client";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";

interface BranchDTO {
  id: string;
  tenant_id: string;
  name: string;
  created_at: string;
}

export function BranchesClient({
  initialBranches,
}: {
  initialBranches: BranchDTO[];
}) {
  const router = useRouter();
  const [branches, setBranches] = React.useState(initialBranches);
  const [newName, setNewName] = React.useState("");
  const [creating, setCreating] = React.useState(false);
  const [pendingDelete, setPendingDelete] = React.useState<{
    id: string;
    name: string;
  } | null>(null);
  const [deleting, setDeleting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function createBranch(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    setCreating(true);
    setError(null);
    try {
      const created = await apiFetch<BranchDTO>(TenantRoutes.branches(), {
        method: "POST",
        body: JSON.stringify({ name: newName.trim() }),
      });
      if (created) {
        setBranches((cur) => [...cur, created]);
        setNewName("");
        router.refresh();
      }
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't create branch.",
      );
    } finally {
      setCreating(false);
    }
  }

  async function renameBranch(id: string, name: string) {
    setError(null);
    try {
      const updated = await apiFetch<BranchDTO>(TenantRoutes.branch(id), {
        method: "PATCH",
        body: JSON.stringify({ name }),
      });
      if (updated) {
        setBranches((cur) => cur.map((b) => (b.id === id ? updated : b)));
      }
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't rename branch.",
      );
    }
  }

  function deleteBranch(id: string, name: string) {
    setPendingDelete({ id, name });
  }

  async function executeDelete() {
    if (!pendingDelete) return;
    const { id } = pendingDelete;
    setPendingDelete(null);
    setDeleting(true);
    setError(null);
    try {
      await apiFetch(TenantRoutes.branch(id), { method: "DELETE" });
      setBranches((cur) => cur.filter((b) => b.id !== id));
      router.refresh();
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't delete branch.",
      );
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="space-y-6">
      <form onSubmit={createBranch} className="flex items-end gap-2">
        <div className="flex-1 space-y-1">
          <Label htmlFor="new-branch">Add a branch</Label>
          <Input
            id="new-branch"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="KL Office, Penang Branch, …"
            maxLength={100}
          />
        </div>
        <Button type="submit" disabled={creating || !newName.trim()}>
          <Plus className="mr-1.5 h-4 w-4" aria-hidden />
          {creating ? "Adding…" : "Add branch"}
        </Button>
      </form>

      {error ? (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : null}

      {branches.length === 0 ? (
        <div className="rounded-xl border border-dashed bg-muted/30 p-8 text-center">
          <h2 className="text-base font-semibold">No branches yet</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Add your first branch above. You can tag knowledge sources and
            audience members to a specific branch so the bot only uses relevant
            content.
          </p>
        </div>
      ) : (
        <ul className="space-y-2">
          {branches.map((b) => (
            <BranchRow
              key={b.id}
              branch={b}
              onRename={renameBranch}
              onDelete={deleteBranch}
            />
          ))}
        </ul>
      )}

      <Dialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete branch?</DialogTitle>
            <DialogDescription>
              &ldquo;{pendingDelete?.name}&rdquo; will be permanently deleted.
              Content tagged to it becomes tenant-wide. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setPendingDelete(null)}>
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
    </div>
  );
}

function BranchRow({
  branch,
  onRename,
  onDelete,
}: {
  branch: BranchDTO;
  onRename: (id: string, name: string) => void;
  onDelete: (id: string, name: string) => void;
}) {
  const [editing, setEditing] = React.useState(false);
  const [name, setName] = React.useState(branch.name);

  return (
    <li className="flex items-center gap-2 rounded-xl border bg-card p-3">
      <MapPin className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
      {editing ? (
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => {
            setEditing(false);
            if (name.trim() && name !== branch.name) {
              onRename(branch.id, name.trim());
            } else {
              setName(branch.name);
            }
          }}
          autoFocus
          className="h-8 flex-1"
          maxLength={100}
        />
      ) : (
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="flex-1 text-left text-sm font-medium hover:underline"
        >
          {branch.name}
        </button>
      )}
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => setEditing(true)}
        aria-label={`Rename ${branch.name}`}
      >
        <Pencil className="h-3.5 w-3.5" aria-hidden />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => onDelete(branch.id, branch.name)}
        aria-label={`Delete ${branch.name}`}
        className="text-destructive hover:bg-destructive/10"
      >
        <Trash2 className="h-3.5 w-3.5" aria-hidden />
      </Button>
    </li>
  );
}
