/**
 * Secrets rotation form (Phase 9 PR-4).
 *
 * Per-service credential rotation. Shows an inline panel with a "Rotate"
 * button; clicking opens a confirmation dialog with the new-value input.
 * On success, shows a "Rotated at {timestamp}" status message inline.
 *
 * Calls POST /api/admin/secrets/{tenant_id}/rotate via adminFetch (proxy).
 *
 * data-testid="rotation-panel-{suffix}" lets E2E specs target each service.
 */

"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card } from "@/components/ui/card";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { formatMyt } from "@/lib/format";

type ServiceKind =
  | "meta_whatsapp"
  | "meta_ig"
  | "meta_messenger"
  | "chatwoot"
  | "telegram_bot_token";

/** Short human label shown in headings and buttons. */
const SERVICE_LABELS: Record<ServiceKind, string> = {
  meta_whatsapp: "WhatsApp",
  meta_ig: "Instagram",
  meta_messenger: "Messenger",
  chatwoot: "Chatwoot",
  telegram_bot_token: "Telegram",
};

/** One-line description of exactly which credential is being rotated. */
const SERVICE_DESCRIPTIONS: Record<ServiceKind, string> = {
  meta_whatsapp:
    "WhatsApp — WABA System User access token (sends WhatsApp messages)",
  meta_messenger: "Messenger — Facebook Page access token (graph.facebook.com)",
  meta_ig:
    "Instagram — Instagram-Login user access token (graph.instagram.com). " +
    "Distinct from the Messenger Page token — do NOT paste a Page token here.",
  telegram_bot_token: "Telegram — Bot token (from BotFather)",
  chatwoot: "Chatwoot — agent API token",
};

/** Label for the "new value" input field, per service. */
const SERVICE_FIELD_LABELS: Record<ServiceKind, string> = {
  meta_whatsapp: "New WhatsApp WABA token",
  meta_messenger: "New Messenger Page access token",
  meta_ig: "New Instagram-Login access token",
  telegram_bot_token: "New Telegram bot token",
  chatwoot: "New Chatwoot agent API token",
};

// E2E-stable testid suffix per service
const TESTID_SUFFIX: Record<ServiceKind, string> = {
  meta_whatsapp: "whatsapp",
  meta_ig: "ig",
  meta_messenger: "messenger",
  chatwoot: "chatwoot",
  telegram_bot_token: "telegram",
};

interface RotateResult {
  service: string;
  rotated_at: string;
}

interface RotationFormProps {
  tenantId: string;
  service: ServiceKind;
}

export function RotationForm({ tenantId, service }: RotationFormProps) {
  const router = useRouter();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [newValue, setNewValue] = useState("");
  const [loading, setLoading] = useState(false);
  const [rotatedAt, setRotatedAt] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!newValue.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await adminFetch<RotateResult>(
        adminRoutes.secrets.rotate(tenantId),
        { method: "POST", body: { service, new_value: newValue } },
      );
      if (res) {
        setRotatedAt(res.rotated_at);
        setNewValue("");
        setDialogOpen(false);
        // Re-fetch the server component so the rotation-history table picks
        // up the new event without a manual page reload.
        router.refresh();
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unexpected error");
    } finally {
      setLoading(false);
    }
  }

  function closeDialog() {
    setDialogOpen(false);
    setNewValue("");
    setError(null);
  }

  return (
    <>
      {/* Inline panel — testid lets E2E locate each service's panel */}
      <Card
        data-testid={`rotation-panel-${TESTID_SUFFIX[service]}`}
        className="space-y-2 p-4"
      >
        <h3 className="text-sm font-semibold">{SERVICE_LABELS[service]}</h3>
        <p className="text-xs text-muted-foreground">
          {SERVICE_DESCRIPTIONS[service]}
        </p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setDialogOpen(true)}
        >
          Rotate {SERVICE_LABELS[service]} token
        </Button>
        {rotatedAt && (
          <p role="status" className="text-sm text-success">
            Rotated at {formatMyt(rotatedAt)}
          </p>
        )}
      </Card>

      {/* Confirmation dialog — Playwright finds via page.getByRole("dialog") */}
      {dialogOpen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby={`rotate-heading-${service}`}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
          onKeyDown={(e) => {
            if (e.key === "Escape") closeDialog();
          }}
        >
          <div className="w-full max-w-sm rounded-lg border bg-background p-6 shadow-lg space-y-4">
            <h2
              id={`rotate-heading-${service}`}
              className="text-sm font-semibold"
            >
              Rotate {SERVICE_LABELS[service]} token
            </h2>
            <p className="text-xs text-muted-foreground">
              {SERVICE_DESCRIPTIONS[service]}
            </p>
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor={`secret-${service}`}>
                  {SERVICE_FIELD_LABELS[service]}
                </Label>
                <Input
                  id={`secret-${service}`}
                  type="password"
                  autoComplete="new-password"
                  value={newValue}
                  onChange={(e) => setNewValue(e.target.value)}
                  placeholder="Paste new credential..."
                  className="font-mono"
                />
              </div>
              {error && <p className="text-sm text-destructive">{error}</p>}
              <div className="flex gap-2">
                <Button
                  type="submit"
                  size="sm"
                  disabled={loading || !newValue.trim()}
                >
                  {loading
                    ? "Rotating..."
                    : `Rotate ${SERVICE_LABELS[service]} token`}
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={closeDialog}
                >
                  Cancel
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}
    </>
  );
}
