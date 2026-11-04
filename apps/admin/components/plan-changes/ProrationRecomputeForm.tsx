"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { formatMyr, formatMytDate } from "@/lib/format";
import type { PlanChangeRequest } from "@/lib/types/billing";
interface ApproveResult {
  invoice_number: string;
  invoice_id: string;
  approved_at: string;
}
interface RejectResult {
  rejected_at: string;
  decision_note: string;
}
interface Props {
  request: PlanChangeRequest;
}

function ProrationSnapshot({
  snapshot,
}: {
  snapshot: Record<string, unknown>;
}) {
  const proratedPrice =
    typeof snapshot.prorated_price_myr === "number"
      ? snapshot.prorated_price_myr
      : null;
  const proratedQuota =
    typeof snapshot.prorated_quota_replies === "number"
      ? snapshot.prorated_quota_replies
      : null;
  const daysRemaining =
    typeof snapshot.days_remaining === "number"
      ? snapshot.days_remaining
      : null;
  const daysInMonth =
    typeof snapshot.days_in_month === "number" ? snapshot.days_in_month : null;
  return (
    <Card className="divide-y divide-border text-sm">
      <div className="px-4 py-2 bg-muted/30">
        <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Proration snapshot
        </span>
      </div>
      {proratedPrice !== null && (
        <div className="flex justify-between px-4 py-2.5">
          <span className="text-muted-foreground">Prorated invoice amount</span>
          <span className="font-mono font-medium">
            {formatMyr(proratedPrice)}
          </span>
        </div>
      )}
      {proratedQuota !== null && (
        <div className="flex justify-between px-4 py-2.5">
          <span className="text-muted-foreground">
            Prorated quota (replies)
          </span>
          <span className="font-mono font-medium">
            {proratedQuota.toLocaleString("en-MY")}
          </span>
        </div>
      )}
      {daysRemaining !== null && daysInMonth !== null && (
        <div className="flex justify-between px-4 py-2.5">
          <span className="text-muted-foreground">Days ratio</span>
          <span className="font-mono font-medium">
            {daysRemaining} / {daysInMonth} days
          </span>
        </div>
      )}
      {proratedPrice === null && proratedQuota === null && (
        <div className="px-4 py-3 text-xs text-muted-foreground">
          No structured snapshot.
          <pre className="mt-1 rounded bg-muted p-2 text-[10px]">
            {JSON.stringify(snapshot, null, 2)}
          </pre>
        </div>
      )}
    </Card>
  );
}
export function ProrationRecomputeForm({ request }: Props) {
  const router = useRouter();
  // Two-step flow: operator first clicks "Recompute proration" to review the
  // snapshot, then Approve / Reject become available.
  const [recomputed, setRecomputed] = useState(false);
  const [approving, setApproving] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [showRejectForm, setShowRejectForm] = useState(false);
  const [decisionNote, setDecisionNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [approveResult, setApproveResult] = useState<ApproveResult | null>(
    null,
  );
  const [rejectResult, setRejectResult] = useState<RejectResult | null>(null);
  const isPending = request.status === "pending";
  async function handleApprove() {
    if (approving) return;
    setApproving(true);
    setError(null);
    try {
      const res = await adminFetch<ApproveResult>(
        adminRoutes.planChanges.approve(request.id),
        { method: "POST" },
      );
      setApproveResult(res ?? null);
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unexpected error");
    } finally {
      setApproving(false);
    }
  }

  async function handleReject() {
    if (rejecting || !decisionNote.trim()) return;
    setRejecting(true);
    setError(null);
    try {
      const res = await adminFetch<RejectResult>(
        adminRoutes.planChanges.reject(request.id),
        {
          method: "POST",
          body: { decision_note: decisionNote },
        },
      );
      setRejectResult(res ?? null);
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unexpected error");
    } finally {
      setRejecting(false);
    }
  }
  if (approveResult) {
    return (
      <Card
        role="status"
        className="border-success/30 bg-success/5 p-5 space-y-1.5 text-sm"
      >
        <p className="font-semibold text-success">Plan change approved.</p>
        <p className="text-muted-foreground">
          Invoice{" "}
          <span className="font-mono font-medium text-foreground">
            {approveResult.invoice_number}
          </span>{" "}
          issued at {formatMytDate(approveResult.approved_at)}.
        </p>
      </Card>
    );
  }
  if (rejectResult) {
    return (
      <Card
        role="status"
        className="border-destructive/30 bg-destructive/5 p-5 space-y-1.5 text-sm"
      >
        <p className="font-semibold text-destructive">Plan change rejected.</p>
        <p className="text-muted-foreground">
          Note:{" "}
          <span className="text-foreground">{rejectResult.decision_note}</span>
        </p>
        <p className="text-xs text-muted-foreground">
          Rejected at {formatMytDate(rejectResult.rejected_at)}.
        </p>
      </Card>
    );
  }
  return (
    <div className="space-y-4">
      {/* Step 1: Recompute button — reveals the proration snapshot */}
      {isPending && !recomputed && (
        <div className="flex items-center gap-3">
          <Button type="button" onClick={() => setRecomputed(true)}>
            Recompute proration
          </Button>
        </div>
      )}

      {/* Step 2: snapshot + Approve / Reject — visible after recompute */}
      {recomputed && (
        <ProrationSnapshot snapshot={request.proration_snapshot ?? {}} />
      )}
      {isPending && recomputed && !showRejectForm && (
        <div className="flex items-center gap-3">
          <Button
            onClick={handleApprove}
            disabled={approving}
            className="bg-success hover:bg-success/90 text-success-foreground"
          >
            {approving ? "Approving…" : "Approve"}
          </Button>
          <Button
            variant="outline"
            className="border-destructive/50 text-destructive hover:bg-destructive/5"
            onClick={() => setShowRejectForm(true)}
          >
            Reject
          </Button>
        </div>
      )}
      {isPending && recomputed && showRejectForm && (
        <Card className="space-y-3 p-4">
          <div className="space-y-1.5">
            <Label htmlFor="decision-note">
              Rejection reason{" "}
              <span className="text-xs text-muted-foreground">(required)</span>
            </Label>
            <textarea
              id="decision-note"
              rows={3}
              value={decisionNote}
              onChange={(e) => setDecisionNote(e.target.value)}
              placeholder="Explain why the plan change is being rejected…"
              className="flex min-h-[80px] w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring resize-none"
            />
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="destructive"
              size="sm"
              disabled={rejecting || !decisionNote.trim()}
              onClick={handleReject}
            >
              {rejecting ? "Rejecting…" : "Confirm reject"}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setShowRejectForm(false)}
            >
              Cancel
            </Button>
          </div>
        </Card>
      )}
      {!isPending && (
        <Card className="border-border bg-muted/30 p-4 text-sm text-muted-foreground">
          This request has already been{" "}
          <span className="capitalize font-medium text-foreground">
            {request.status}
          </span>
          . No further action is available.
        </Card>
      )}
      <p
        role="alert"
        aria-live="assertive"
        aria-atomic="true"
        className="text-sm text-destructive"
      >
        {error ?? ""}
      </p>
    </div>
  );
}
