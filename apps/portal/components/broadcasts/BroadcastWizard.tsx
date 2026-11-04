"use client";

/**
 * Broadcast composer wizard (Phase 8 PR-9).
 *
 * 3 steps:
 *   1. Choose template (approved-only list).
 *   2. Pick audience (channel + tag filter).
 *   3. Schedule (now / future datetime) + confirm.
 *
 * Submits via `POST /api/tenant/broadcasts/` — CreateBroadcastRequest
 * (packages/shared-types/python/polaris_shared_types/broadcast.py).
 * Backend materialises recipients + enqueues via the existing Phase 6
 * worker drain (no new dispatch code).
 *
 * WIRE-SHAPE FIX (X1, audit-batch-f-frontend-consolidation 2026-07):
 * this form previously sent `{kind:"channel", channel}` (singular,
 * wrong key name) and was missing `name`/`tenant_channel_id` entirely —
 * both required fields on CreateBroadcastRequest — so every submission
 * 422'd or 500'd before this fix. Corrected to the real shape:
 * `filter_kind` (not `kind`) + `channels: [...]` (a list, not a
 * singular `channel`) for FilterChannel, `parts` (not `filters`) for
 * FilterCombinedAnd, `scheduled_at` (not `scheduled_for`), plus the
 * two missing required fields.
 */

import * as React from "react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiFetch } from "@/lib/api/client";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { cn } from "@/lib/utils";
import {
  BROADCAST_CAPABLE_KINDS,
  CHANNEL_KIND_LABELS,
} from "@/lib/channel-kinds";

interface TemplateRow {
  id: string;
  name: string;
  language: string;
  status: string;
}

interface ChannelRow {
  id: string;
  channel: "whatsapp" | "instagram" | "messenger";
  status: string;
}

// Backend-enforced audience-filter subset (FilterChannel.channels:
// list[Literal["whatsapp","messenger"]], max_length=2) — derived from the
// canonical module so this list can't offer a kind the API will 422 on
// (Instagram was never valid here; this is a correctness fix riding along
// with F1's vocab consolidation, not purely cosmetic — the picker
// previously offered Instagram, an option the API always rejected).
const CHANNEL_OPTIONS = BROADCAST_CAPABLE_KINDS.map((value) => ({
  value,
  label: CHANNEL_KIND_LABELS[value],
}));
type FilterChannelKind = (typeof BROADCAST_CAPABLE_KINDS)[number];

type AudienceFilterPayload =
  | { filter_kind: "channel"; channels: FilterChannelKind[] }
  | {
      filter_kind: "combined_and";
      parts: (
        | { filter_kind: "channel"; channels: FilterChannelKind[] }
        | { filter_kind: "tags_any"; tags: string[] }
      )[];
    };

export function BroadcastWizard({
  templates,
  channels,
}: {
  templates: TemplateRow[];
  channels: ChannelRow[];
}) {
  const router = useRouter();
  const [step, setStep] = React.useState<1 | 2 | 3>(1);
  const [templateId, setTemplateId] = React.useState(templates[0]?.id ?? "");
  const [tenantChannelId, setTenantChannelId] = React.useState(
    channels[0]?.id ?? "",
  );
  const [name, setName] = React.useState("");
  const [channel, setChannel] = React.useState<FilterChannelKind>("whatsapp");
  const [tagsCsv, setTagsCsv] = React.useState("");
  const [whenMode, setWhenMode] = React.useState<"now" | "future">("now");
  const [scheduledFor, setScheduledFor] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const tags = tagsCsv
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);

  function buildFilter(): AudienceFilterPayload {
    if (tags.length === 0) {
      return { filter_kind: "channel", channels: [channel] };
    }
    return {
      filter_kind: "combined_and",
      parts: [
        { filter_kind: "channel", channels: [channel] },
        { filter_kind: "tags_any", tags },
      ],
    };
  }

  async function submit() {
    setSubmitting(true);
    setError(null);
    try {
      await apiFetch(TenantRoutes.broadcasts(), {
        method: "POST",
        body: JSON.stringify({
          name: name.trim(),
          tenant_channel_id: tenantChannelId,
          template_id: templateId,
          audience_filter: buildFilter(),
          scheduled_at:
            whenMode === "future" && scheduledFor
              ? new Date(scheduledFor).toISOString()
              : null,
        }),
      });
      router.push("/broadcasts");
      router.refresh();
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't create the broadcast.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-6">
      <ol className="flex items-center gap-2 text-sm" aria-label="Steps">
        {[1, 2, 3].map((s) => (
          <li
            key={s}
            aria-current={step === s ? "step" : undefined}
            className={cn(
              "rounded-full border px-3 py-1",
              step === s
                ? "border-foreground bg-foreground text-background"
                : "border-border text-muted-foreground",
            )}
          >
            {s}.{" "}
            {s === 1 ? "Template" : s === 2 ? "Audience" : "Schedule & send"}
          </li>
        ))}
      </ol>

      {step === 1 ? (
        <section className="space-y-3">
          <div className="space-y-2">
            <Label htmlFor="bcast-name">Broadcast name</Label>
            <Input
              id="bcast-name"
              className="max-w-md"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="June promo — VIP customers"
              maxLength={200}
              required
            />
            <p className="text-xs text-muted-foreground">
              Internal label only — recipients never see this.
            </p>
          </div>

          {channels.length > 1 ? (
            <div className="space-y-2">
              <Label htmlFor="bcast-channel-inbox">WhatsApp inbox</Label>
              <select
                id="bcast-channel-inbox"
                className="w-full max-w-md rounded-lg bg-background px-2 py-1.5 text-sm outline outline-1 outline-input transition-colors hover:outline-n-slate-6"
                value={tenantChannelId}
                onChange={(e) => setTenantChannelId(e.target.value)}
              >
                {channels.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.id.slice(0, 8)}…
                  </option>
                ))}
              </select>
            </div>
          ) : null}

          <div className="space-y-2">
            <Label htmlFor="bcast-template">Approved template</Label>
            <select
              id="bcast-template"
              className="w-full max-w-md rounded-lg bg-background px-2 py-1.5 text-sm outline outline-1 outline-input transition-colors hover:outline-n-slate-6"
              value={templateId}
              onChange={(e) => setTemplateId(e.target.value)}
            >
              {templates.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name} ({t.language})
                </option>
              ))}
            </select>
          </div>

          <div className="flex justify-end">
            <Button
              onClick={() => setStep(2)}
              disabled={!templateId || !tenantChannelId || !name.trim()}
            >
              Next: Audience
            </Button>
          </div>
        </section>
      ) : null}

      {step === 2 ? (
        <section className="space-y-3">
          <div className="space-y-2">
            <Label htmlFor="bcast-channel">Channel</Label>
            <select
              id="bcast-channel"
              className="w-full max-w-[240px] rounded-lg bg-background px-2 py-1.5 text-sm outline outline-1 outline-input transition-colors hover:outline-n-slate-6"
              value={channel}
              onChange={(e) => setChannel(e.target.value as typeof channel)}
            >
              {CHANNEL_OPTIONS.map((c) => (
                <option key={c.value} value={c.value}>
                  {c.label}
                </option>
              ))}
            </select>
          </div>

          <div className="space-y-2">
            <Label htmlFor="bcast-tags">
              Tags (any-match, comma-separated)
            </Label>
            <Input
              id="bcast-tags"
              value={tagsCsv}
              onChange={(e) => setTagsCsv(e.target.value)}
              placeholder="vip, returning_customer"
            />
            <p className="text-xs text-muted-foreground">
              Leave empty to target everyone opted-in on the chosen channel.
            </p>
          </div>

          <div className="flex justify-between">
            <Button variant="outline" onClick={() => setStep(1)}>
              Back
            </Button>
            <Button onClick={() => setStep(3)}>Next: Schedule</Button>
          </div>
        </section>
      ) : null}

      {step === 3 ? (
        <section className="space-y-3">
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">When</legend>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name="when"
                value="now"
                checked={whenMode === "now"}
                onChange={() => setWhenMode("now")}
              />
              Send immediately after I confirm
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name="when"
                value="future"
                checked={whenMode === "future"}
                onChange={() => setWhenMode("future")}
              />
              Schedule for later
            </label>
            {whenMode === "future" ? (
              <Input
                type="datetime-local"
                value={scheduledFor}
                onChange={(e) => setScheduledFor(e.target.value)}
                className="max-w-[260px]"
              />
            ) : null}
          </fieldset>

          {error ? (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          ) : null}

          <div className="flex justify-between">
            <Button variant="outline" onClick={() => setStep(2)}>
              Back
            </Button>
            <Button
              onClick={submit}
              disabled={submitting || (whenMode === "future" && !scheduledFor)}
            >
              {submitting
                ? "Creating…"
                : whenMode === "now"
                  ? "Send broadcast"
                  : "Schedule broadcast"}
            </Button>
          </div>
        </section>
      ) : null}
    </div>
  );
}
