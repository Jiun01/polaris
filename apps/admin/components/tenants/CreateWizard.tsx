/**
 * Tenant create wizard (Phase 9 PR-3, Deliverable #1 — sales-led signup
 * per ADR-012).
 *
 * 4 steps:
 *   1. Business profile  — display_name, slug, contact, vertical
 *   2. Plan              — tier + billing_cycle + optional sales_quote_id
 *   3. Admin user        — email + name (magic-link dispatched on submit)
 *   4. Confirmation      — review + "Create tenant & send link" CTA
 *
 * State machine: linear, step `0..3`. Back is always available; Next is
 * disabled until per-step validation passes. Submit fires the API
 * call; success → toast + redirect to the new tenant's detail page.
 */

"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { adminRoutes } from "@/lib/api/routes";
import { adminFetch } from "@/lib/api/client";
import { ApiError } from "@/lib/api/error";
import { cn } from "@/lib/utils";
import type { PlanTier } from "@/components/tenants/types";
import {
  ALL_CHANNEL_KINDS,
  CHANNEL_KIND_LABELS,
  tierDefaultChannels,
  type ChannelKind,
} from "@/lib/channel-kinds";

type BillingCycle = "monthly" | "annual";

function sameKinds(a: readonly string[], b: readonly string[]): boolean {
  if (a.length !== b.length) return false;
  const sa = [...a].sort();
  const sb = [...b].sort();
  return sa.every((v, i) => v === sb[i]);
}

interface FormState {
  display_name: string;
  slug: string;
  contact_email: string;
  contact_phone: string;
  vertical: string;
  plan_tier: PlanTier;
  billing_cycle: BillingCycle;
  sales_quote_id: string;
  admin_email: string;
  admin_display_name: string;
  ea_email: string; // EA calendar login; blank → defaults to admin_email on submit
  chatwoot_account_id: string; // stored as string in the input, parsed to int on submit
  allowed_channel_kinds: ChannelKind[];
}

const INITIAL: FormState = {
  display_name: "",
  slug: "",
  contact_email: "",
  contact_phone: "",
  vertical: "",
  plan_tier: "starter",
  billing_cycle: "monthly",
  sales_quote_id: "",
  admin_email: "",
  admin_display_name: "",
  ea_email: "",
  chatwoot_account_id: "",
  allowed_channel_kinds: tierDefaultChannels("starter"),
};

const STEPS = [
  { label: "Business profile", short: "Profile" },
  { label: "Plan", short: "Plan" },
  { label: "Admin user", short: "Admin" },
  { label: "Confirm", short: "Confirm" },
] as const;

const TIERS: readonly PlanTier[] = ["starter", "pro", "ultra", "enterprise"];

function slugify(input: string): string {
  return input
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
}

function isValidEmail(s: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(s);
}

function isValidSlug(s: string): boolean {
  return /^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$/.test(s);
}

interface StepCheck {
  ok: boolean;
  hint?: string;
}

function checkStep(step: number, f: FormState): StepCheck {
  if (step === 0) {
    if (!f.display_name.trim())
      return { ok: false, hint: "Display name required." };
    if (!isValidSlug(f.slug))
      return {
        ok: false,
        hint: "Slug must be lowercase, 3–48 chars, [a-z0-9-].",
      };
    if (f.contact_email && !isValidEmail(f.contact_email))
      return { ok: false, hint: "Contact email is malformed." };
    return { ok: true };
  }
  if (step === 1) {
    if (!TIERS.includes(f.plan_tier))
      return { ok: false, hint: "Pick a tier." };
    if (f.allowed_channel_kinds.length === 0)
      return { ok: false, hint: "Select at least one channel." };
    return { ok: true };
  }
  if (step === 2) {
    if (!isValidEmail(f.admin_email))
      return { ok: false, hint: "Admin email is required and must be valid." };
    if (!f.admin_display_name.trim())
      return { ok: false, hint: "Admin display name required." };
    // EA email is optional — blank falls back to the admin email on submit.
    if (f.ea_email.trim() && !isValidEmail(f.ea_email))
      return { ok: false, hint: "EA email must be valid." };
    return { ok: true };
  }
  return { ok: true };
}

interface CreateResponse {
  id: string;
  slug: string;
  magic_link_dispatched: boolean;
  magic_link_failure_reason: string | null;
  ea_account_provisioned?: boolean;
}

export function CreateTenantWizard() {
  const router = useRouter();
  const [step, setStep] = useState(0);
  const [form, setForm] = useState<FormState>(INITIAL);
  const [submitting, setSubmitting] = useState(false);
  // touchedSteps: tracks which steps the user has interacted with.
  // Validation hints are suppressed until a field on that step is changed,
  // preventing "field required" messages appearing before any user input.
  const [touchedSteps, setTouchedSteps] = useState<Set<number>>(new Set());

  const check = checkStep(step, form);
  const isLast = step === STEPS.length - 1;

  // Mark the current step as touched (idempotent) so validation hints surface
  // only after the user has interacted with that step.
  const markStepTouched = () => {
    setTouchedSteps((prev) => {
      if (prev.has(step)) return prev;
      const next = new Set(prev);
      next.add(step);
      return next;
    });
  };

  const setField = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    markStepTouched();
    setForm((prev) => {
      const next = { ...prev, [key]: value };
      // Auto-slug when display_name is typed and the user hasn't
      // touched slug yet (slug still equals slugify(prev.display_name)
      // OR is empty).
      if (
        key === "display_name" &&
        (!prev.slug || prev.slug === slugify(prev.display_name))
      ) {
        next.slug = slugify(String(value));
      }
      // When the tier changes, reset channel entitlements to that tier's default.
      if (key === "plan_tier") {
        next.allowed_channel_kinds = tierDefaultChannels(value as PlanTier);
      }
      return next;
    });
  };

  const toggleChannel = (kind: ChannelKind) => {
    markStepTouched();
    setForm((prev) => {
      const has = prev.allowed_channel_kinds.includes(kind);
      const nextKinds = has
        ? prev.allowed_channel_kinds.filter((k) => k !== kind)
        : [...prev.allowed_channel_kinds, kind];
      return { ...prev, allowed_channel_kinds: nextKinds };
    });
  };

  async function submit() {
    setSubmitting(true);
    try {
      const cwAccountId = parseInt(form.chatwoot_account_id, 10);
      // Send the channel override ONLY when it diverges from the tier default —
      // omitted → the API applies the tier default.
      const tierDefault = tierDefaultChannels(form.plan_tier);
      const channelsDiverged = !sameKinds(
        form.allowed_channel_kinds,
        tierDefault,
      );
      const payload = {
        display_name: form.display_name.trim(),
        slug: form.slug,
        contact_email: form.contact_email.trim() || null,
        contact_phone: form.contact_phone.trim() || null,
        vertical: form.vertical.trim() || null,
        plan_tier: form.plan_tier,
        billing_cycle: form.billing_cycle,
        sales_quote_id: form.sales_quote_id.trim() || null,
        admin_email: form.admin_email.trim().toLowerCase(),
        admin_display_name: form.admin_display_name.trim(),
        ea_email: (
          form.ea_email.trim() || form.admin_email.trim()
        ).toLowerCase(),
        chatwoot_account_id:
          !isNaN(cwAccountId) && cwAccountId >= 1 ? cwAccountId : null,
        ...(channelsDiverged
          ? { allowed_channel_kinds: form.allowed_channel_kinds }
          : {}),
      };
      const res = await adminFetch<CreateResponse>(
        adminRoutes.tenants.create(),
        { method: "POST", body: payload },
      );
      if (!res) {
        // 204 from a POST that should return JSON — API contract drift.
        toast.error("Tenant create returned an empty response.");
        setSubmitting(false);
        return;
      }
      if (res.magic_link_dispatched) {
        toast.success(
          `Tenant ${res.slug} created. Magic-link sent to ${form.admin_email}.`,
        );
      } else {
        // Token row may still be committed (per portal_invite.py).
        // Operator sees the failure_reason so they know whether to
        // direct the user manually to /login.
        const reason = res.magic_link_failure_reason ?? "unknown";
        toast.warning(
          `Tenant ${res.slug} created, but magic-link dispatch did not complete (${reason}). ` +
            `Direct ${form.admin_email} to portal.getpolaris.my/login to request a fresh link.`,
        );
      }
      router.push(`/tenants/${res.id}`);
    } catch (err) {
      const msg =
        err instanceof ApiError ? err.message : "Failed to create tenant.";
      toast.error(msg);
      setSubmitting(false);
    }
  }

  return (
    <Card className="space-y-6 p-6">
      <Stepper current={step} />

      {step === 0 && (
        <div className="space-y-4">
          <Field label="Business name" htmlFor="display_name">
            <Input
              id="display_name"
              value={form.display_name}
              onChange={(e) => setField("display_name", e.target.value)}
              placeholder="Acme Salon"
              autoFocus
            />
          </Field>
          <Field
            label="Slug"
            htmlFor="slug"
            hint="Used in subdomain + invoice numbers. Lowercase, 3–48 chars."
          >
            <Input
              id="slug"
              value={form.slug}
              onChange={(e) => setField("slug", e.target.value)}
              placeholder="acme-salon"
            />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Contact email" htmlFor="contact_email">
              <Input
                id="contact_email"
                type="email"
                value={form.contact_email}
                onChange={(e) => setField("contact_email", e.target.value)}
                placeholder="ops@acme.my"
              />
            </Field>
            <Field label="Contact phone" htmlFor="contact_phone">
              <Input
                id="contact_phone"
                value={form.contact_phone}
                onChange={(e) => setField("contact_phone", e.target.value)}
                placeholder="+60 12 345 6789"
              />
            </Field>
          </div>
          <Field
            label="Vertical"
            htmlFor="vertical"
            hint="Optional. Retail / Salon / Dental / F&B…"
          >
            <Input
              id="vertical"
              value={form.vertical}
              onChange={(e) => setField("vertical", e.target.value)}
              placeholder="Salon"
            />
          </Field>
          <Field
            label="Chatwoot account ID"
            htmlFor="chatwoot_account_id"
            hint="Required for inbox auto-fill and email config. Found in Chatwoot → Settings → Account."
          >
            <Input
              id="chatwoot_account_id"
              type="number"
              min={1}
              value={form.chatwoot_account_id}
              onChange={(e) => setField("chatwoot_account_id", e.target.value)}
              placeholder="e.g. 1"
            />
          </Field>
        </div>
      )}

      {step === 1 && (
        <div className="space-y-4">
          <div>
            <Label className="mb-2 block">Tier</Label>
            <div
              role="radiogroup"
              aria-label="Tier"
              className="grid grid-cols-2 gap-2 md:grid-cols-4"
            >
              {TIERS.map((t) => {
                const active = form.plan_tier === t;
                return (
                  <label
                    key={t}
                    className={cn(
                      "flex cursor-pointer items-center justify-center rounded-md border px-3 py-2 text-sm font-medium capitalize transition",
                      active
                        ? "border-primary bg-primary/10 text-primary"
                        : "border-border bg-background text-muted-foreground hover:bg-muted hover:text-foreground",
                    )}
                  >
                    <input
                      type="radio"
                      name="plan_tier"
                      value={t}
                      checked={active}
                      onChange={() => setField("plan_tier", t)}
                      className="sr-only"
                    />
                    {t}
                  </label>
                );
              })}
            </div>
          </div>
          <div>
            <Label className="mb-2 block">Billing cycle</Label>
            <div className="grid grid-cols-2 gap-2">
              {(["monthly", "annual"] as const).map((c) => {
                const active = form.billing_cycle === c;
                return (
                  <button
                    key={c}
                    type="button"
                    onClick={() => setField("billing_cycle", c)}
                    className={cn(
                      "rounded-md border px-3 py-2 text-sm font-medium capitalize transition",
                      active
                        ? "border-primary bg-primary/10 text-primary"
                        : "border-border bg-background text-muted-foreground hover:bg-muted hover:text-foreground",
                    )}
                  >
                    {c}
                  </button>
                );
              })}
            </div>
          </div>
          <div>
            <div className="mb-2 flex items-center justify-between">
              <Label className="block">Allowed channels</Label>
              {!sameKinds(
                form.allowed_channel_kinds,
                tierDefaultChannels(form.plan_tier),
              ) && (
                <span className="rounded bg-warning/15 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-warning">
                  customised
                </span>
              )}
            </div>
            <p className="mb-2 text-xs text-muted-foreground">
              Defaults from the tier. Uncheck to restrict, or add channels
              beyond the default. Auto-provisioning honours this list.
            </p>
            <div
              role="group"
              aria-label="Allowed channels"
              className="grid grid-cols-2 gap-2 md:grid-cols-3"
            >
              {ALL_CHANNEL_KINDS.map((k) => {
                const checked = form.allowed_channel_kinds.includes(k);
                return (
                  <label
                    key={k}
                    className={cn(
                      "flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm transition",
                      checked
                        ? "border-primary bg-primary/10 text-foreground"
                        : "border-border bg-background text-muted-foreground hover:bg-muted",
                    )}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleChannel(k)}
                      className="h-4 w-4 rounded border-border"
                    />
                    {CHANNEL_KIND_LABELS[k]}
                  </label>
                );
              })}
            </div>
          </div>
          <Field
            label="Sales quote ID"
            htmlFor="sales_quote_id"
            hint="Optional. POL-Q-… code from a generated quote (Phase 9 PR-9)."
          >
            <Input
              id="sales_quote_id"
              value={form.sales_quote_id}
              onChange={(e) => setField("sales_quote_id", e.target.value)}
              placeholder="POL-Q-26060001"
            />
          </Field>
        </div>
      )}

      {step === 2 && (
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            The tenant admin receives a Polaris portal magic-link on submit.
            Magic-link expires after 15 minutes.
          </p>
          <Field label="Admin email" htmlFor="admin_email">
            <Input
              id="admin_email"
              type="email"
              value={form.admin_email}
              onChange={(e) => setField("admin_email", e.target.value)}
              placeholder="jane@acme.my"
              autoFocus
            />
          </Field>
          <Field label="Admin display name" htmlFor="admin_display_name">
            <Input
              id="admin_display_name"
              value={form.admin_display_name}
              onChange={(e) => setField("admin_display_name", e.target.value)}
              placeholder="Jane Tan"
            />
          </Field>
          <Field
            label="EA (calendar) email"
            htmlFor="ea_email"
            hint="Gets an Easy!Appointments password-reset link to set their own password. Defaults to the admin email if left blank."
          >
            <Input
              id="ea_email"
              type="email"
              value={form.ea_email}
              onChange={(e) => setField("ea_email", e.target.value)}
              placeholder={form.admin_email || "ops@acme.my"}
            />
          </Field>
        </div>
      )}

      {step === 3 && (
        <div className="space-y-3 text-sm">
          <h2 className="text-base font-semibold">Confirm details</h2>
          <Summary label="Display name" value={form.display_name} />
          <Summary label="Slug" value={form.slug} />
          <Summary
            label="Chatwoot account ID"
            value={form.chatwoot_account_id || "—"}
          />
          <Summary label="Contact" value={form.contact_email || "—"} />
          <Summary label="Vertical" value={form.vertical || "—"} />
          <Separator />
          <Summary
            label="Plan"
            value={`${form.plan_tier} (${form.billing_cycle})`}
          />
          <Summary
            label="Channels"
            value={form.allowed_channel_kinds
              .map((k) => CHANNEL_KIND_LABELS[k])
              .join(", ")}
          />
          <Summary label="Quote ref" value={form.sales_quote_id || "—"} />
          <Separator />
          <Summary label="Admin email" value={form.admin_email} />
          <Summary label="Admin name" value={form.admin_display_name} />
          <Summary
            label="EA email"
            value={form.ea_email.trim() || form.admin_email || "—"}
          />
          <p className="pt-2 text-xs text-muted-foreground">
            Submitting will create the tenant + tenant_users row, dispatch a
            portal magic-link to <strong>{form.admin_email || "—"}</strong>, and
            email an Easy!Appointments password-reset link to{" "}
            <strong>{form.ea_email.trim() || form.admin_email || "—"}</strong>.
          </p>
        </div>
      )}

      {!check.ok && check.hint && touchedSteps.has(step) && (
        <p className="text-xs text-destructive" role="alert">
          {check.hint}
        </p>
      )}

      <div className="flex items-center justify-between">
        <Button
          type="button"
          variant="ghost"
          disabled={step === 0 || submitting}
          onClick={() => setStep((s) => Math.max(0, s - 1))}
        >
          Back
        </Button>
        {isLast ? (
          <Button
            type="button"
            disabled={!check.ok || submitting}
            onClick={submit}
          >
            {submitting ? "Creating…" : "Create tenant & send link"}
          </Button>
        ) : (
          <Button
            type="button"
            onClick={() => {
              // Always mark the step touched so the validation hint becomes
              // visible — previously the button was `disabled` when invalid,
              // so clicking it on a pristine form did nothing with no on-screen
              // reason (QA finding L9). Only advance when the step is valid.
              markStepTouched();
              if (check.ok) {
                setStep((s) => Math.min(STEPS.length - 1, s + 1));
              }
            }}
          >
            Next
          </Button>
        )}
      </div>
    </Card>
  );
}

function Stepper({ current }: { current: number }) {
  return (
    <ol className="flex items-center gap-2 text-xs">
      {STEPS.map((s, i) => {
        const state =
          i < current ? "done" : i === current ? "current" : "pending";
        return (
          <li key={s.label} className="flex items-center gap-2">
            <span
              className={cn(
                "flex h-6 w-6 items-center justify-center rounded-full border text-[10px] font-semibold",
                state === "done" &&
                  "border-primary bg-primary text-primary-foreground",
                state === "current" &&
                  "border-primary bg-background text-primary",
                state === "pending" &&
                  "border-border bg-background text-muted-foreground",
              )}
            >
              {i + 1}
            </span>
            <span
              className={cn(
                "font-medium",
                state === "current"
                  ? "text-foreground"
                  : "text-muted-foreground",
              )}
            >
              {s.short}
            </span>
            {i < STEPS.length - 1 && (
              <span className="mx-1 h-px w-6 bg-border" aria-hidden />
            )}
          </li>
        );
      })}
    </ol>
  );
}

function Field({
  label,
  htmlFor,
  hint,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function Summary({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <span className="text-xs uppercase tracking-wider text-muted-foreground">
        {label}
      </span>
      <span className="font-medium text-foreground">{value || "—"}</span>
    </div>
  );
}
