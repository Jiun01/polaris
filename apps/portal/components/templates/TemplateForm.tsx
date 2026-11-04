"use client";

/**
 * Multi-step template compose form (Phase 8 PR-8).
 *
 * Three steps: Category → Compose (header / body / footer with {{n}}
 * placeholders) → Preview → Submit. Submission POSTs to
 * `/api/tenant/templates/` with the Meta wire shape. On success
 * routes to /templates with a refresh so the new pending row appears.
 *
 * Variable detection: scans body text for `{{1}}`, `{{2}}`, ... and
 * surfaces an `example.body_text` array so Meta has sample fills to
 * judge the template content.
 */

import * as React from "react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { apiFetch } from "@/lib/api/client";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { cn } from "@/lib/utils";

type Category = "MARKETING" | "UTILITY" | "AUTHENTICATION";

// Display-only shape describing a tenant_channels row passed in as a
// channel-picker prop — NOT an allowed-to-submit set. Templates are
// validated WhatsApp-only at submission (tenant_templates.py's
// TEMPLATE_CHANNEL_NOT_WHATSAPP), and the caller (templates/new/page.tsx)
// already pre-filters to `channel === "whatsapp"` rows only; this 3-kind
// union is describing what a tenant_channels row CAN look like, not
// restricting the form. See plan Decision #4 — not worth a named
// TEMPLATE_CAPABLE_KINDS constant for one kind.
interface ChannelDTO {
  id: string;
  channel: "whatsapp" | "instagram" | "messenger";
}

// AUTHENTICATION is intentionally NOT offered here: Meta pre-formats auth
// templates and the {{1}} OTP wiring is handled outside this self-serve form.
// The Category type still includes it so existing auth templates render fine.
const CATEGORIES: { value: Category; label: string; hint: string }[] = [
  {
    value: "UTILITY",
    label: "Utility",
    hint: "Order updates, booking confirmations, OTP follow-ups.",
  },
  {
    value: "MARKETING",
    label: "Marketing",
    hint: "Promotions, announcements. Higher review bar.",
  },
];

function detectPlaceholders(text: string): string[] {
  const matches = Array.from(text.matchAll(/\{\{(\d+)\}\}/g));
  const ids = matches.map((m) => m[1]);
  return Array.from(new Set(ids)).sort((a, b) => Number(a) - Number(b));
}

export function TemplateForm({ channels }: { channels: ChannelDTO[] }) {
  const router = useRouter();
  const [step, setStep] = React.useState<1 | 2 | 3>(1);
  const [channelId, setChannelId] = React.useState(channels[0]?.id ?? "");
  const [name, setName] = React.useState("");
  const [language, setLanguage] = React.useState("en");
  const [category, setCategory] = React.useState<Category>("UTILITY");
  const [headerText, setHeaderText] = React.useState("");
  const [bodyText, setBodyText] = React.useState("");
  const [footerText, setFooterText] = React.useState("");
  const [exampleValues, setExampleValues] = React.useState<
    Record<string, string>
  >({});
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const placeholders = detectPlaceholders(bodyText);

  function buildComponents(): unknown[] {
    const components: unknown[] = [];
    if (headerText.trim()) {
      components.push({
        type: "HEADER",
        format: "TEXT",
        text: headerText.trim(),
      });
    }
    const body: Record<string, unknown> = {
      type: "BODY",
      text: bodyText.trim(),
    };
    if (placeholders.length > 0) {
      const fills = placeholders.map((p) => exampleValues[p] ?? "");
      body.example = { body_text: [fills] };
    }
    components.push(body);
    if (footerText.trim()) {
      components.push({
        type: "FOOTER",
        text: footerText.trim(),
      });
    }
    return components;
  }

  async function submit() {
    setSubmitting(true);
    setError(null);
    try {
      await apiFetch(TenantRoutes.templates(), {
        method: "POST",
        body: JSON.stringify({
          tenant_channel_id: channelId,
          name: name.trim(),
          language,
          category,
          components: buildComponents(),
        }),
      });
      router.push("/templates");
      router.refresh();
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Submission failed. Meta rejected the template shape. Try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  const nameEmpty = name.trim().length === 0;
  const nameInvalid = !nameEmpty && !/^[a-z0-9_]{1,512}$/.test(name);
  const canStep2 =
    /^[a-z0-9_]{1,512}$/.test(name) &&
    language.length >= 2 &&
    Boolean(channelId);
  const canStep3 = bodyText.trim().length > 0;

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
            {s === 1 ? "Category" : s === 2 ? "Compose" : "Preview & submit"}
          </li>
        ))}
      </ol>

      {step === 1 ? (
        <section className="space-y-4">
          {channels.length > 1 ? (
            <div className="space-y-2">
              <Label htmlFor="tpl-channel">WhatsApp inbox</Label>
              <select
                id="tpl-channel"
                className="w-full max-w-md rounded-lg bg-background px-2 py-1.5 text-sm outline outline-1 outline-input transition-colors hover:outline-n-slate-6"
                value={channelId}
                onChange={(e) => setChannelId(e.target.value)}
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
            <Label htmlFor="tpl-name">Template name (lower_snake)</Label>
            <Input
              id="tpl-name"
              className="max-w-md"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                setError(null);
              }}
              placeholder="order_confirmation"
              required
              pattern="[a-z0-9_]+"
            />
            {nameEmpty && error === "Template name is required." ? (
              <p role="alert" className="text-xs text-destructive">
                Template name is required.
              </p>
            ) : nameInvalid ? (
              <p role="alert" className="text-xs text-destructive">
                Use lowercase letters, numbers, and underscores only.
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                Lowercase letters, digits, and underscores only. Meta requires
                this format.
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="tpl-lang">Language code</Label>
            <Input
              id="tpl-lang"
              className="max-w-[160px]"
              value={language}
              onChange={(e) => setLanguage(e.target.value)}
              placeholder="en"
            />
            <p className="text-xs text-muted-foreground">
              Meta locale: <code className="rounded bg-muted px-1">en</code>,{" "}
              <code className="rounded bg-muted px-1">ms_MY</code>,{" "}
              <code className="rounded bg-muted px-1">zh_CN</code>.
            </p>
          </div>

          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">Category</legend>
            <div className="grid gap-3 sm:grid-cols-3">
              {CATEGORIES.map((c) => (
                <label
                  key={c.value}
                  className={cn(
                    "cursor-pointer rounded-xl border p-3 text-sm",
                    category === c.value
                      ? "border-foreground bg-accent/40"
                      : "border-border bg-card hover:border-foreground/30",
                  )}
                >
                  <input
                    type="radio"
                    name="tpl-cat"
                    value={c.value}
                    checked={category === c.value}
                    onChange={() => setCategory(c.value)}
                    className="sr-only"
                  />
                  <div className="font-medium">{c.label}</div>
                  <p className="mt-1 text-xs text-muted-foreground">{c.hint}</p>
                </label>
              ))}
            </div>
          </fieldset>

          <div className="flex justify-end">
            <Button
              onClick={() => {
                if (nameEmpty) {
                  setError("Template name is required.");
                  return;
                }
                if (canStep2) setStep(2);
              }}
              disabled={nameInvalid}
            >
              Next: Compose
            </Button>
          </div>
        </section>
      ) : null}

      {step === 2 ? (
        <section className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="tpl-header">Header (optional)</Label>
            <Input
              id="tpl-header"
              value={headerText}
              onChange={(e) => setHeaderText(e.target.value)}
              maxLength={60}
            />
            <p className="text-xs text-muted-foreground">
              Plain text header up to 60 characters.
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="tpl-body">Body</Label>
            <Textarea
              id="tpl-body"
              rows={6}
              value={bodyText}
              onChange={(e) => setBodyText(e.target.value)}
              placeholder="Hi {{1}}, your order {{2}} is on its way."
            />
            <p className="text-xs text-muted-foreground">
              Use <code className="rounded bg-muted px-1">{"{{1}}"}</code>,{" "}
              <code className="rounded bg-muted px-1">{"{{2}}"}</code>, … for
              variables.
            </p>
          </div>

          {placeholders.length > 0 ? (
            <div className="space-y-2 rounded-lg border bg-muted/20 p-3">
              <h3 className="text-xs font-medium uppercase text-muted-foreground">
                Example fills (for Meta review)
              </h3>
              <div className="grid gap-2 sm:grid-cols-2">
                {placeholders.map((p) => (
                  <div key={p} className="space-y-1">
                    <Label htmlFor={`ex-${p}`}>{`{{${p}}}`}</Label>
                    <Input
                      id={`ex-${p}`}
                      value={exampleValues[p] ?? ""}
                      onChange={(e) =>
                        setExampleValues((cur) => ({
                          ...cur,
                          [p]: e.target.value,
                        }))
                      }
                    />
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          <div className="space-y-2">
            <Label htmlFor="tpl-footer">Footer (optional)</Label>
            <Input
              id="tpl-footer"
              value={footerText}
              onChange={(e) => setFooterText(e.target.value)}
              maxLength={60}
            />
          </div>

          <div className="flex justify-between">
            <Button variant="outline" onClick={() => setStep(1)}>
              Back
            </Button>
            <Button onClick={() => setStep(3)} disabled={!canStep3}>
              Next: Preview
            </Button>
          </div>
        </section>
      ) : null}

      {step === 3 ? (
        <section className="space-y-4">
          <div className="rounded-xl border bg-card p-4">
            <div className="text-xs uppercase text-muted-foreground">
              Preview
            </div>
            {/* WhatsApp-bubble mockup — emerald deliberately mimics the
                WhatsApp brand green for an authentic preview, not a raw-
                palette straggler; shadow-sm dropped (absent from the
                current token system). */}
            <div className="mt-2 max-w-md rounded-2xl bg-emerald-50 p-3 text-sm dark:bg-emerald-950">
              {headerText ? (
                <div className="mb-1 font-semibold">{headerText}</div>
              ) : null}
              <p className="whitespace-pre-wrap">
                {previewWithFills(bodyText, exampleValues)}
              </p>
              {footerText ? (
                <div className="mt-2 text-xs text-muted-foreground">
                  {footerText}
                </div>
              ) : null}
            </div>
          </div>

          <div className="text-xs text-muted-foreground">
            <strong>Note:</strong> Meta reviews each template before it becomes
            sendable. Approval is typically &lt; 24 hours; you&apos;ll see
            status updates on the Templates list.
          </div>

          {error ? (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          ) : null}

          <div className="flex justify-between">
            <Button variant="outline" onClick={() => setStep(2)}>
              Back
            </Button>
            <Button onClick={submit} disabled={submitting}>
              {submitting ? "Submitting…" : "Submit to Meta"}
            </Button>
          </div>
        </section>
      ) : null}
    </div>
  );
}

function previewWithFills(body: string, fills: Record<string, string>): string {
  return body.replace(/\{\{(\d+)\}\}/g, (_, n) => fills[n] || `{{${n}}}`);
}
