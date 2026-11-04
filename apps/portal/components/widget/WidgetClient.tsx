"use client";

/**
 * Website-widget config form (off-Meta channels M4).
 *
 * Self-serve (all tiers): toggle the bubble on, set a greeting, add channel
 * entries (each a deep link into t.me / wa.me / m.me / ig.me), and copy the
 * embed snippet. Saving PUTs to /api/tenant/widget; the backend re-validates
 * every target against the chat-host allowlist (422 surfaced inline).
 */

import * as React from "react";
import { Plus, Trash2, Copy, Check } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { apiFetch } from "@/lib/api/client";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";

export interface WidgetChannel {
  kind: string;
  label: string;
  target: string;
}

export interface WidgetConfig {
  widget_key: string;
  enabled: boolean;
  greeting: string;
  channels: WidgetChannel[];
  embed_snippet: string;
}

const MAX_CHANNELS = 6;

export function WidgetClient({ initial }: { initial: WidgetConfig }) {
  const [enabled, setEnabled] = React.useState(initial.enabled);
  const [greeting, setGreeting] = React.useState(initial.greeting);
  const [channels, setChannels] = React.useState<WidgetChannel[]>(
    initial.channels,
  );
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [saved, setSaved] = React.useState(false);
  const [copied, setCopied] = React.useState(false);

  function updateChannel(i: number, patch: Partial<WidgetChannel>) {
    setChannels((cs) =>
      cs.map((c, idx) => (idx === i ? { ...c, ...patch } : c)),
    );
  }

  function addChannel() {
    if (channels.length >= MAX_CHANNELS) return;
    // "telegram" is a plain default value, not sourced from lib/channel-kinds
    // — WidgetChannel.kind is untyped `string` (deep-link kind, incl.
    // non-ChannelKind values like ig.me), not worth importing the module
    // for a single default literal. See plan Mandatory Reading note.
    setChannels((cs) => [...cs, { kind: "telegram", label: "", target: "" }]);
  }

  function removeChannel(i: number) {
    setChannels((cs) => cs.filter((_, idx) => idx !== i));
  }

  async function save() {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      await apiFetch<WidgetConfig>(TenantRoutes.widget(), {
        method: "PUT",
        body: JSON.stringify({ enabled, greeting, channels }),
      });
      setSaved(true);
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Could not save widget settings.",
      );
    } finally {
      setSaving(false);
    }
  }

  async function copySnippet() {
    try {
      await navigator.clipboard.writeText(initial.embed_snippet);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — no-op */
    }
  }

  return (
    <div className="space-y-8">
      {/* Enable + greeting */}
      <section className="space-y-4 rounded-lg border p-5">
        <div className="flex items-center gap-3">
          <Switch
            id="widget-enabled"
            checked={enabled}
            onCheckedChange={(checked) => setEnabled(checked)}
          />
          <Label htmlFor="widget-enabled" className="text-sm font-medium">
            Enable the website widget
          </Label>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="greeting">Greeting</Label>
          <Input
            id="greeting"
            value={greeting}
            maxLength={80}
            onChange={(e) => setGreeting(e.target.value)}
            placeholder="Chat with us"
          />
        </div>
      </section>

      {/* Channels */}
      <section className="space-y-4 rounded-lg border p-5">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">Channels in the bubble</h2>
          <Button
            variant="outline"
            size="sm"
            onClick={addChannel}
            disabled={channels.length >= MAX_CHANNELS}
          >
            <Plus className="mr-1 h-4 w-4" /> Add channel
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          Each link must point to t.me, wa.me, m.me, or ig.me (e.g.
          <code className="mx-1">https://t.me/your_bot</code> or
          <code className="mx-1">https://wa.me/60123456789</code>).
        </p>

        {channels.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No channels yet. Add one to show it in the bubble.
          </p>
        ) : (
          <div className="space-y-3">
            {channels.map((c, i) => (
              <div key={i} className="grid grid-cols-12 items-end gap-2">
                <div className="col-span-3 space-y-1">
                  <Label htmlFor={`kind-${i}`}>Kind</Label>
                  <Input
                    id={`kind-${i}`}
                    value={c.kind}
                    onChange={(e) => updateChannel(i, { kind: e.target.value })}
                  />
                </div>
                <div className="col-span-3 space-y-1">
                  <Label htmlFor={`label-${i}`}>Label</Label>
                  <Input
                    id={`label-${i}`}
                    value={c.label}
                    onChange={(e) =>
                      updateChannel(i, { label: e.target.value })
                    }
                  />
                </div>
                <div className="col-span-5 space-y-1">
                  <Label htmlFor={`target-${i}`}>Link</Label>
                  <Input
                    id={`target-${i}`}
                    value={c.target}
                    onChange={(e) =>
                      updateChannel(i, { target: e.target.value })
                    }
                    placeholder="https://t.me/your_bot"
                  />
                </div>
                <div className="col-span-1">
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Remove channel"
                    onClick={() => removeChannel(i)}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {error ? (
        <div
          role="alert"
          className="rounded-lg border border-destructive bg-destructive/10 px-4 py-3 text-sm text-destructive"
        >
          {error}
        </div>
      ) : null}

      <div className="flex items-center gap-3">
        <Button onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save changes"}
        </Button>
        {saved ? (
          <span className="text-sm text-muted-foreground">Saved.</span>
        ) : null}
      </div>

      {/* Embed snippet */}
      <section className="space-y-2 rounded-lg border p-5">
        <h2 className="text-sm font-semibold">Embed on your website</h2>
        <p className="text-xs text-muted-foreground">
          Paste this once before <code>&lt;/body&gt;</code>. Changes you save
          here update the live bubble, so there&apos;s no need to re-paste.
        </p>
        <div className="flex items-center gap-2">
          <code className="flex-1 overflow-x-auto rounded bg-muted px-3 py-2 text-xs">
            {initial.embed_snippet}
          </code>
          <Button variant="outline" size="sm" onClick={copySnippet}>
            {copied ? (
              <Check className="h-4 w-4" />
            ) : (
              <Copy className="h-4 w-4" />
            )}
          </Button>
        </div>
      </section>
    </div>
  );
}
