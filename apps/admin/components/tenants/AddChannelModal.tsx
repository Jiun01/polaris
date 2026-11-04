/**
 * AddChannelModal — Phase 10 PR-0-retrofit.
 *
 * Platform_admin can add a channel to a tenant with an optional
 * tier-cap override. ADR-015 Amendment 3rd.
 *
 * Security invariants (mirrored server-side):
 *   1. The override checkbox is NEVER rendered when `isImpersonating` is
 *      true (admin opened a portal tab as a tenant user).
 *   2. The reason textarea is only shown when the override checkbox is
 *      checked — it becomes required for form submission.
 *   3. Submit is disabled until chatwoot_inbox_id is filled in AND (if
 *      override=true) a non-empty reason is entered.
 *
 * Channel kinds available:
 *   - Starter:            whatsapp only (unless override=true → all GA kinds)
 *   - Pro/Ultra/Enterprise: all GA kinds
 *     (whatsapp, instagram_login, messenger, telegram, email)
 *
 * Email is Chatwoot-native: no access token and no Meta/Telegram IDs — only the
 * Chatwoot inbox id. IMAP/SMTP credentials live in Chatwoot; the bot posts draft
 * replies as private notes. The server (provision_channel) attaches the Polaris
 * Bot to the inbox on add, same as every other kind.
 */

"use client";

import { useState, useEffect } from "react";
import { PlusCircle } from "lucide-react";

import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import {
  ALL_CHANNEL_KINDS,
  CHANNEL_KIND_LABELS,
  tierDefaultChannels,
  type ChannelKind,
} from "@/lib/channel-kinds";
import type { PlanTier } from "@/components/tenants/types";

// ----------------------------------------------------------------- types ----

interface ChannelAddResponse {
  channel_id: string;
  channel_kind: ChannelKind;
  override_applied: boolean;
  /** True when the backend successfully captured the Meta access token from Chatwoot. */
  token_captured?: boolean;
}

interface InboxInfoResult {
  inbox_name: string | null;
  channel_type: string;
  waba_id: string | null;
  phone_number_id: string | null;
  ig_business_account_id: string | null;
  fb_page_id: string | null;
  telegram_bot_name: string | null;
  /** Already-masked last-4 preview (e.g. "••••3a2f") or null if not retrievable. Never the full token. */
  token_preview?: string | null;
  error: string | null;
}

interface Props {
  tenantId: string;
  planTier: PlanTier | null;
  /** True when the admin currently has an impersonation session active. */
  isImpersonating: boolean;
  /** Called after a successful channel add so the parent can revalidate. */
  onSuccess: (channelKind: string) => void;
}

// ---------------------------------------------------------------- helpers ----

function allowedKinds(tier: PlanTier | null): ChannelKind[] {
  return tierDefaultChannels(tier);
}

function kindLabel(kind: ChannelKind | string): string {
  return (CHANNEL_KIND_LABELS as Record<string, string>)[kind] ?? kind;
}

/**
 * Returns a read-only status note explaining how the access token is handled
 * for the selected channel kind.  Returns null for kinds that have no
 * server-side token capture (Telegram, Email).
 */
function tokenStatusNote(kind: ChannelKind): string | null {
  if (kind === "whatsapp") {
    return "Access token will be captured automatically from Chatwoot on save.";
  }
  if (kind === "messenger") {
    return "Access token will be captured from Chatwoot (Embedded Signup) on save.";
  }
  if (kind === "instagram_login") {
    return "Instagram-Login access token will be captured from Chatwoot on save.";
  }
  if (kind === "email") {
    return "Email (IMAP/SMTP) credentials are managed in Chatwoot — no token is stored in Polaris. The bot drafts replies as private notes for an agent to review.";
  }
  return null;
}

/**
 * Returns a muted fallback hint when token_preview is null, explaining why
 * no preview is available for the given channel kind.
 */
function tokenPreviewFallback(kind: ChannelKind): string {
  if (kind === "whatsapp" || kind === "telegram") {
    return "No token stored yet";
  }
  if (kind === "instagram_login") {
    return "Captured from Chatwoot on save — not retrievable afterward";
  }
  // messenger — Embedded Signup flow, token is never returned by Chatwoot API
  return "Captured via Embedded Signup — not retrievable from Chatwoot";
}

/**
 * The "Chatwoot inbox ID" input + its lookup feedback. Rendered once for the
 * Telegram branch (above the bot token) and once for non-Telegram channels; the
 * `id` prop keeps the two instances' HTML ids distinct (they previously both
 * hard-coded `ac-cw-id`). `showBotName` adds the Telegram bot handle to the
 * resolved-inbox line.
 */
function InboxIdField({
  id,
  value,
  onChange,
  lookupState,
  lookupResult,
  showBotName,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  lookupState: "idle" | "loading" | "done" | "error";
  lookupResult: InboxInfoResult | null;
  showBotName?: boolean;
}) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>
        Chatwoot inbox ID{" "}
        <span className="text-xs text-muted-foreground">(required)</span>
      </Label>
      <Input
        id={id}
        type="number"
        min={1}
        placeholder="e.g. 42"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        required
      />
      {lookupState === "loading" && (
        <p className="text-xs text-muted-foreground">Looking up inbox…</p>
      )}
      {lookupState === "done" && lookupResult?.inbox_name && (
        <p className="text-xs text-muted-foreground">
          Inbox:{" "}
          <span className="font-medium text-foreground">
            {lookupResult.inbox_name}
          </span>
          {showBotName && lookupResult.telegram_bot_name && (
            <> · Bot: @{lookupResult.telegram_bot_name}</>
          )}
        </p>
      )}
      {lookupState === "error" && (
        <p className="text-xs text-amber-700 dark:text-amber-400">
          Could not fetch inbox details from Chatwoot — fill fields manually.
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- component ----

export function AddChannelModal({
  tenantId,
  planTier,
  isImpersonating,
  onSuccess,
}: Props) {
  const [open, setOpen] = useState(false);

  // Form state
  const [channelKind, setChannelKind] = useState<ChannelKind>("whatsapp");
  const [chatwootInboxId, setChatwootInboxId] = useState("");
  const [wabaId, setWabaId] = useState("");
  const [phoneNumberId, setPhoneNumberId] = useState("");
  const [fbPageId, setFbPageId] = useState("");
  const [igBusinessId, setIgBusinessId] = useState("");
  const [overrideTierCap, setOverrideTierCap] = useState(false);
  const [overrideReason, setOverrideReason] = useState("");

  const [botToken, setBotToken] = useState("");

  const [lookupState, setLookupState] = useState<
    "idle" | "loading" | "done" | "error"
  >("idle");
  const [lookupResult, setLookupResult] = useState<InboxInfoResult | null>(
    null,
  );

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // When override is toggled on, expand the kind list to all GA kinds.
  const availableKinds: ChannelKind[] = overrideTierCap
    ? [...ALL_CHANNEL_KINDS]
    : allowedKinds(planTier);

  // Ensure current kind selection is still valid when override changes.
  function handleOverrideToggle(checked: boolean) {
    setOverrideTierCap(checked);
    if (!checked) {
      const still = allowedKinds(planTier);
      if (!still.includes(channelKind)) {
        setChannelKind(still[0]);
      }
      setOverrideReason("");
    }
  }

  useEffect(() => {
    const id = parseInt(chatwootInboxId, 10);
    if (!chatwootInboxId.trim() || isNaN(id) || id < 1) {
      setLookupState("idle");
      setLookupResult(null);
      return;
    }
    setLookupState("loading");
    const timer = setTimeout(async () => {
      try {
        const data = await adminFetch<InboxInfoResult>(
          adminRoutes.tenants.inboxInfo(tenantId, id),
        );
        if (data) {
          setLookupResult(data);
          setLookupState(data.error ? "error" : "done");
          if (!data.error) {
            if (data.waba_id) setWabaId(data.waba_id);
            if (data.phone_number_id) setPhoneNumberId(data.phone_number_id);
            if (data.ig_business_account_id)
              setIgBusinessId(data.ig_business_account_id);
            if (data.fb_page_id) setFbPageId(data.fb_page_id);
          }
        }
      } catch {
        setLookupState("error");
        setLookupResult(null);
      }
    }, 400);
    return () => clearTimeout(timer);
  }, [chatwootInboxId, tenantId]);

  const kindMismatch =
    lookupResult !== null &&
    lookupResult.channel_type !== "unknown" &&
    lookupResult.channel_type !== channelKind;

  const canSubmit =
    chatwootInboxId.trim() !== "" &&
    (channelKind !== "telegram" || botToken.trim() !== "") &&
    (!overrideTierCap || overrideReason.trim() !== "");

  const tokenNote = tokenStatusNote(channelKind);

  function resetForm() {
    setChannelKind("whatsapp");
    setChatwootInboxId("");
    setWabaId("");
    setPhoneNumberId("");
    setFbPageId("");
    setIgBusinessId("");
    setBotToken("");
    setOverrideTierCap(false);
    setOverrideReason("");
    setLookupState("idle");
    setLookupResult(null);
    setError(null);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;

    setSubmitting(true);
    setError(null);

    try {
      if (channelKind === "telegram") {
        await adminFetch(adminRoutes.tenants.connectTelegram(tenantId), {
          method: "POST",
          body: {
            bot_token: botToken.trim(),
            chatwoot_inbox_id: parseInt(chatwootInboxId, 10),
            override_tier_cap: overrideTierCap,
            override_reason: overrideTierCap ? overrideReason.trim() : null,
          },
        });
        setOpen(false);
        resetForm();
        onSuccess("telegram");
      } else {
        const connectionPayload: Record<string, unknown> = {
          chatwoot_inbox_id: parseInt(chatwootInboxId, 10),
        };
        if (channelKind === "whatsapp") {
          if (wabaId.trim()) connectionPayload.waba_id = wabaId.trim();
          if (phoneNumberId.trim())
            connectionPayload.phone_number_id = phoneNumberId.trim();
        } else if (channelKind === "instagram_login") {
          if (igBusinessId.trim())
            connectionPayload.ig_business_account_id = igBusinessId.trim();
        } else if (channelKind === "messenger") {
          if (fbPageId.trim()) connectionPayload.fb_page_id = fbPageId.trim();
        }
        const result = await adminFetch<ChannelAddResponse>(
          adminRoutes.tenants.addChannel(tenantId),
          {
            method: "POST",
            body: {
              channel_kind: channelKind,
              connection_payload: connectionPayload,
              override_tier_cap: overrideTierCap,
              override_reason: overrideTierCap ? overrideReason.trim() : null,
            },
          },
        );
        if (result) {
          setOpen(false);
          resetForm();
          const label = kindLabel(result.channel_kind);
          if (result.token_captured === false) {
            toast.warning(
              `${label} channel added, but token capture failed — set it on the Secrets page.`,
            );
          } else {
            const capturedSuffix =
              result.token_captured === true ? " Token captured ✓" : "";
            toast.success(`${label} channel added.${capturedSuffix}`);
          }
          onSuccess(result.channel_kind);
        }
      }
    } catch (err) {
      if (err instanceof ApiError) {
        setError(
          err.message ||
            `API error ${err.status} — check your inputs and try again.`,
        );
      } else {
        setError("Unexpected error — please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        setOpen(v);
        if (!v) resetForm();
      }}
    >
      <DialogTrigger asChild>
        <Button size="sm" variant="outline" className="gap-1.5">
          <PlusCircle className="h-3.5 w-3.5" aria-hidden />
          Add channel
        </Button>
      </DialogTrigger>

      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add channel</DialogTitle>
          <DialogDescription>
            Connect a new channel inbox to this tenant. The Chatwoot inbox must
            be created first via the Chatwoot Platform API.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Channel kind */}
          <div className="space-y-1.5">
            <Label htmlFor="ac-kind">Channel kind</Label>
            <select
              id="ac-kind"
              value={channelKind}
              onChange={(e) => setChannelKind(e.target.value as ChannelKind)}
              className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              {availableKinds.map((k) => (
                <option key={k} value={k}>
                  {kindLabel(k)}
                </option>
              ))}
            </select>
          </div>

          {kindMismatch && (
            <p className="text-xs text-amber-700 dark:text-amber-400">
              This Chatwoot inbox is a{" "}
              <strong>{kindLabel(lookupResult!.channel_type)}</strong> inbox —
              you have <strong>{kindLabel(channelKind)}</strong> selected.
            </p>
          )}

          {/* Token-handling status note (Meta channels only) */}
          {tokenNote !== null && (
            <p
              role="status"
              aria-live="polite"
              aria-label="Token handling"
              className="text-xs text-muted-foreground rounded-md border border-border bg-muted/40 px-3 py-2"
            >
              {tokenNote}
            </p>
          )}

          {/* Chatwoot inbox ID — for Telegram this comes FIRST (above the bot token) */}
          {channelKind === "telegram" && (
            <InboxIdField
              id="ac-cw-id-telegram"
              value={chatwootInboxId}
              onChange={setChatwootInboxId}
              lookupState={lookupState}
              lookupResult={lookupResult}
              showBotName
            />
          )}

          {/* Telegram bot token */}
          {channelKind === "telegram" && (
            <div className="space-y-1.5">
              <Label htmlFor="ac-tg-token">
                Bot token{" "}
                <span className="text-xs text-muted-foreground">
                  (required — from @BotFather)
                </span>
              </Label>
              <Input
                id="ac-tg-token"
                placeholder="e.g. 123456789:AAF..."
                value={botToken}
                onChange={(e) => setBotToken(e.target.value)}
                required
              />
            </div>
          )}

          {/* Chatwoot inbox ID — non-Telegram channels (rendered after the token note) */}
          {channelKind !== "telegram" && (
            <InboxIdField
              id="ac-cw-id"
              value={chatwootInboxId}
              onChange={setChatwootInboxId}
              lookupState={lookupState}
              lookupResult={lookupResult}
            />
          )}

          {/* Token preview — shown after successful inbox-info lookup.
              Suppressed for email: Chatwoot-native, no access token to preview. */}
          {lookupState === "done" &&
            !lookupResult?.error &&
            channelKind !== "email" && (
              <div
                className="space-y-0.5"
                aria-label={`Access token preview for ${kindLabel(channelKind)} channel`}
              >
                <p className="text-xs font-medium text-muted-foreground">
                  Access token
                </p>
                {lookupResult?.token_preview ? (
                  <p
                    aria-label="Masked access token preview"
                    className="font-mono text-xs text-foreground"
                  >
                    {lookupResult.token_preview}
                  </p>
                ) : (
                  <p
                    aria-label="Access token not available"
                    className="text-xs text-muted-foreground italic"
                  >
                    {tokenPreviewFallback(channelKind)}
                  </p>
                )}
              </div>
            )}

          {/* WhatsApp-specific fields */}
          {channelKind === "whatsapp" && (
            <>
              <div className="space-y-1.5">
                <Label htmlFor="ac-waba">WABA ID</Label>
                <Input
                  id="ac-waba"
                  placeholder="e.g. 123456789012345"
                  value={wabaId}
                  onChange={(e) => setWabaId(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="ac-phone">Phone number ID</Label>
                <Input
                  id="ac-phone"
                  placeholder="e.g. 987654321098765"
                  value={phoneNumberId}
                  onChange={(e) => setPhoneNumberId(e.target.value)}
                />
              </div>
            </>
          )}

          {/* Instagram-specific fields (IG-Login uses ig_business_account_id) */}
          {channelKind === "instagram_login" && (
            <div className="space-y-1.5">
              <Label htmlFor="ac-ig">Instagram Business Account ID</Label>
              <Input
                id="ac-ig"
                placeholder="e.g. 10000000000000001"
                value={igBusinessId}
                onChange={(e) => setIgBusinessId(e.target.value)}
              />
            </div>
          )}

          {/* Messenger-specific fields */}
          {channelKind === "messenger" && (
            <div className="space-y-1.5">
              <Label htmlFor="ac-fb">Facebook Page ID</Label>
              <Input
                id="ac-fb"
                placeholder="e.g. 200000000000001"
                value={fbPageId}
                onChange={(e) => setFbPageId(e.target.value)}
              />
            </div>
          )}

          {/* Override tier cap — hidden during impersonation */}
          {!isImpersonating && (
            <div className="space-y-3 rounded-md border border-amber-200 bg-amber-50 p-3 dark:border-amber-800 dark:bg-amber-950/20">
              <div className="flex items-center gap-2">
                <input
                  id="ac-override"
                  type="checkbox"
                  checked={overrideTierCap}
                  onChange={(e) => handleOverrideToggle(e.target.checked)}
                  className="h-4 w-4 rounded border-border"
                />
                <Label
                  htmlFor="ac-override"
                  className="text-sm font-medium text-amber-900 dark:text-amber-200"
                >
                  Override tier cap
                </Label>
              </div>
              <p className="text-xs text-amber-800 dark:text-amber-300">
                Bypasses{" "}
                <code className="rounded bg-amber-100 px-1 dark:bg-amber-900">
                  allowed_channel_kinds
                </code>{" "}
                and the Starter 1-inbox cap. An audit row is always written.
                Review overrides quarterly via the
                ../docs/internal-ops/RUNBOOK.md query.
              </p>

              {/* Reason — required when override is checked (invariant #2) */}
              {overrideTierCap && (
                <div className="space-y-1.5">
                  <Label
                    htmlFor="ac-reason"
                    className="text-sm text-amber-900 dark:text-amber-200"
                  >
                    Reason{" "}
                    <span className="text-xs opacity-70">(required)</span>
                  </Label>
                  <textarea
                    id="ac-reason"
                    rows={2}
                    placeholder="e.g. Sales exception for ACME Co — approved by operator 2026-06-01"
                    value={overrideReason}
                    onChange={(e) => setOverrideReason(e.target.value)}
                    className="flex min-h-[60px] w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    required
                  />
                </div>
              )}
            </div>
          )}

          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => setOpen(false)}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={!canSubmit || submitting}>
              {submitting ? "Adding…" : "Add channel"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
