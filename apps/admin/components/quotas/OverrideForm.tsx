"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { ALL_CHANNEL_KINDS, CHANNEL_KIND_LABELS } from "@/lib/channel-kinds";

export interface QuotaValues {
  bot_replies: number;
  bot_replies_period_cap: number;
  vision_calls_per_month: number;
  conversation_retention_days: number;
  knowledge_chunks_total_mb: number;
  rate_limit_per_minute: number;
  monthly_llm_tokens_in: number;
  monthly_llm_tokens_out: number;
  monthly_ingestion_pages: number;
  monthly_broadcast_recipients: number;
  monthly_bookings: number;
  agent_seats_included: number;
  agent_seats_extra: number;
  outbound_broadcast_enabled: boolean;
  multi_branch_enabled: boolean;
  allowed_channel_kinds: string[];
  usage_bot_replies_this_period: number;
  usage_pct: number;
}

type FieldType = "number" | "boolean" | "channels";

interface FieldDef {
  key: string;
  label: string;
  description: string;
  type: FieldType;
  unit?: string;
}

interface TabDef {
  id: string;
  label: string;
  fields: FieldDef[];
}

const TABS: TabDef[] = [
  {
    id: "replies",
    label: "Bot Replies",
    fields: [
      {
        key: "bot_replies",
        label: "Monthly Cap",
        description:
          "Total bot replies this tenant can send per calendar month. Enterprise tenants default to 0 — set the contracted amount here.",
        type: "number",
      },
      {
        key: "bot_replies_period_cap",
        label: "Period Cap Override",
        description:
          "Real-time hard cap enforced by the Redis counter within a rolling window. Normally matches the monthly cap; lower it to limit burst traffic.",
        type: "number",
      },
    ],
  },
  {
    id: "channels",
    label: "Channels & Features",
    fields: [
      {
        key: "outbound_broadcast_enabled",
        label: "Broadcast Enabled",
        description:
          "Allow this tenant to run outbound broadcast campaigns to their contacts.",
        type: "boolean",
      },
      {
        key: "multi_branch_enabled",
        label: "Multi-Branch",
        description:
          "Allow this tenant to configure and manage multiple branches or locations.",
        type: "boolean",
      },
      {
        key: "allowed_channel_kinds",
        label: "Allowed Channels",
        description:
          "Channels this tenant can activate. Toggle to add or remove. Takes effect on the next channel add — existing provisioned inboxes are not removed. Auto-provisioning on first inbound honours this list.",
        type: "channels",
      },
    ],
  },
  {
    id: "storage",
    label: "Storage & Content",
    fields: [
      {
        key: "knowledge_chunks_total_mb",
        label: "Knowledge Base Limit",
        description:
          "Maximum storage for the tenant's embedded knowledge base (vector chunks).",
        type: "number",
        unit: "MB",
      },
      {
        key: "monthly_ingestion_pages",
        label: "Ingestion Pages / Month",
        description:
          "Maximum document pages that can be ingested and embedded per calendar month.",
        type: "number",
        unit: "pages",
      },
      {
        key: "vision_calls_per_month",
        label: "Vision Calls / Month",
        description:
          "Maximum image-analysis (vision model) API calls the tenant can make per month.",
        type: "number",
        unit: "calls",
      },
      {
        key: "conversation_retention_days",
        label: "Conversation Retention",
        description:
          "Days of conversation history kept before pruning. Affects the tenant's history and audit views.",
        type: "number",
        unit: "days",
      },
    ],
  },
  {
    id: "rate",
    label: "Rate Limits & Tokens",
    fields: [
      {
        key: "rate_limit_per_minute",
        label: "API Rate Limit",
        description:
          "Maximum Polaris API requests this tenant can send per minute across all their channels.",
        type: "number",
        unit: "req/min",
      },
      {
        key: "monthly_llm_tokens_in",
        label: "Input Tokens / Month",
        description:
          "Maximum LLM prompt tokens (user messages + context) consumed per month.",
        type: "number",
        unit: "tokens",
      },
      {
        key: "monthly_llm_tokens_out",
        label: "Output Tokens / Month",
        description:
          "Maximum LLM completion tokens (bot replies) generated per month.",
        type: "number",
        unit: "tokens",
      },
    ],
  },
  {
    id: "campaigns",
    label: "Campaigns & Bookings",
    fields: [
      {
        key: "monthly_broadcast_recipients",
        label: "Broadcast Recipients / Month",
        description:
          "Maximum unique contacts reachable across all broadcast campaigns per month.",
        type: "number",
      },
      {
        key: "monthly_bookings",
        label: "Bookings / Month",
        description:
          "Maximum appointment bookings managed through this tenant per calendar month.",
        type: "number",
      },
    ],
  },
  {
    id: "seats",
    label: "Seats",
    fields: [
      {
        key: "agent_seats_included",
        label: "Included Agent Seats",
        description: "Human-agent Chatwoot seats bundled with this plan tier.",
        type: "number",
        unit: "seats",
      },
      {
        key: "agent_seats_extra",
        label: "Extra Agent Seats",
        description:
          "Additional purchased agent seats added on top of the included allocation.",
        type: "number",
        unit: "seats",
      },
    ],
  },
];

interface FieldStatus {
  loading: boolean;
  saved: boolean;
  error: string | null;
}

function initDrafts(quota: QuotaValues): Record<string, string> {
  const init: Record<string, string> = {};
  TABS.forEach((tab) => {
    tab.fields.forEach((f) => {
      if (f.type !== "number" && f.type !== "boolean") return;
      const raw = quota[f.key as keyof QuotaValues];
      init[f.key] =
        f.type === "boolean"
          ? (raw as boolean)
            ? "1"
            : "0"
          : String(raw as number);
    });
  });
  return init;
}

function displayCurrentValue(field: FieldDef, quota: QuotaValues): string {
  const raw = quota[field.key as keyof QuotaValues];
  if (field.type === "boolean")
    return (raw as boolean) ? "Enabled" : "Disabled";
  if (field.type === "channels") return "";
  return (raw as number).toLocaleString();
}

function SaveButton({
  status,
  onSave,
}: {
  status: FieldStatus | undefined;
  onSave: () => void;
}) {
  if (status?.saved) {
    return (
      <span className="text-xs text-success font-medium whitespace-nowrap">
        Saved ✓
      </span>
    );
  }
  return (
    <Button type="button" size="sm" disabled={status?.loading} onClick={onSave}>
      {status?.loading ? "Saving…" : "Save"}
    </Button>
  );
}

export function OverrideForm({
  tenantId,
  quota,
}: {
  tenantId: string;
  quota: QuotaValues;
}) {
  const [activeTab, setActiveTab] = useState(TABS[0].id);
  const [drafts, setDrafts] = useState<Record<string, string>>(() =>
    initDrafts(quota),
  );
  const [statuses, setStatuses] = useState<Record<string, FieldStatus>>({});
  // allowed_channel_kinds is an array column — held separately from the string
  // `drafts` map and saved via its own handler (not the int parseInt path).
  const [channels, setChannels] = useState<string[]>(() => [
    ...(quota.allowed_channel_kinds ?? []),
  ]);
  const [channelStatus, setChannelStatus] = useState<FieldStatus | undefined>(
    undefined,
  );

  function toggleChannel(kind: string) {
    setChannelStatus(undefined);
    setChannels((prev) =>
      prev.includes(kind) ? prev.filter((k) => k !== kind) : [...prev, kind],
    );
  }

  async function saveChannels() {
    setChannelStatus({ loading: true, saved: false, error: null });
    try {
      await adminFetch(adminRoutes.quotas.override(tenantId), {
        method: "PUT",
        body: { column: "allowed_channel_kinds", value: channels },
      });
      setChannelStatus({ loading: false, saved: true, error: null });
      setTimeout(
        () => setChannelStatus((p) => (p ? { ...p, saved: false } : p)),
        3000,
      );
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Unexpected error";
      setChannelStatus({ loading: false, saved: false, error: msg });
    }
  }

  async function saveField(field: FieldDef) {
    const draft = drafts[field.key] ?? "";
    setStatuses((prev) => ({
      ...prev,
      [field.key]: { loading: true, saved: false, error: null },
    }));
    try {
      const parsed = draft === "" ? null : parseInt(draft, 10);
      await adminFetch(adminRoutes.quotas.override(tenantId), {
        method: "PUT",
        body: { column: field.key, value: parsed },
      });
      setStatuses((prev) => ({
        ...prev,
        [field.key]: { loading: false, saved: true, error: null },
      }));
      setTimeout(() => {
        setStatuses((prev) => ({
          ...prev,
          [field.key]: { ...prev[field.key], saved: false },
        }));
      }, 3000);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Unexpected error";
      setStatuses((prev) => ({
        ...prev,
        [field.key]: { loading: false, saved: false, error: msg },
      }));
    }
  }

  const currentTab = TABS.find((t) => t.id === activeTab) ?? TABS[0];

  return (
    <div className="space-y-4">
      {/* Tab bar */}
      <div className="flex flex-wrap gap-0.5 border-b border-border">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => setActiveTab(tab.id)}
            className={cn(
              "px-3 py-2 text-sm rounded-t transition-colors",
              activeTab === tab.id
                ? "border-b-2 border-primary font-medium text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Field list */}
      <div className="space-y-3">
        {currentTab.fields.map((field) => {
          const status = statuses[field.key];
          const currentDisplay = displayCurrentValue(field, quota);

          return (
            <Card key={field.key} className="p-4">
              <div className="flex items-start justify-between gap-6">
                {/* Label + description + current value */}
                <div className="flex-1 min-w-0 space-y-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium">{field.label}</span>
                    {field.unit && (
                      <Badge variant="outline" className="text-xs font-mono">
                        {field.unit}
                      </Badge>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground leading-relaxed">
                    {field.description}
                  </p>
                  {field.type !== "channels" && (
                    <p className="text-xs text-muted-foreground">
                      Current:{" "}
                      <span className="font-mono font-medium text-foreground">
                        {currentDisplay}
                      </span>
                    </p>
                  )}
                </div>

                {/* Control */}
                {field.type === "channels" ? (
                  <div className="flex shrink-0 flex-col items-end gap-2">
                    <div className="flex max-w-[300px] flex-wrap justify-end gap-1.5">
                      {ALL_CHANNEL_KINDS.map((k) => {
                        const on = channels.includes(k);
                        return (
                          <button
                            key={k}
                            type="button"
                            aria-pressed={on}
                            onClick={() => toggleChannel(k)}
                            className={cn(
                              "rounded-full border px-2.5 py-1 text-xs font-medium transition",
                              on
                                ? "border-primary bg-primary/10 text-primary"
                                : "border-border bg-background text-muted-foreground hover:bg-muted",
                            )}
                          >
                            {CHANNEL_KIND_LABELS[k] ?? k}
                          </button>
                        );
                      })}
                    </div>
                    <SaveButton status={channelStatus} onSave={saveChannels} />
                    {channelStatus?.error && (
                      <p className="text-xs text-destructive">
                        {channelStatus.error}
                      </p>
                    )}
                  </div>
                ) : field.type === "boolean" ? (
                  <div className="flex items-center gap-2 shrink-0">
                    <select
                      value={drafts[field.key] ?? "0"}
                      onChange={(e) =>
                        setDrafts((prev) => ({
                          ...prev,
                          [field.key]: e.target.value,
                        }))
                      }
                      className="flex h-9 rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    >
                      <option value="1">Enabled</option>
                      <option value="0">Disabled</option>
                    </select>
                    <SaveButton
                      status={status}
                      onSave={() => saveField(field)}
                    />
                  </div>
                ) : (
                  <div className="flex items-center gap-2 shrink-0">
                    <Input
                      type="number"
                      min={0}
                      value={drafts[field.key] ?? ""}
                      onChange={(e) =>
                        setDrafts((prev) => ({
                          ...prev,
                          [field.key]: e.target.value,
                        }))
                      }
                      className="w-32 font-mono"
                    />
                    <SaveButton
                      status={status}
                      onSave={() => saveField(field)}
                    />
                  </div>
                )}
              </div>
              {status?.error && (
                <p className="mt-2 text-xs text-destructive">{status.error}</p>
              )}
            </Card>
          );
        })}
      </div>
    </div>
  );
}
