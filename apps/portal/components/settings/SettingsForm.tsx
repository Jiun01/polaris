"use client";

/**
 * Settings sectioned form (Phase 8 PR-4).
 *
 * One <form> per editable section so a partial save lands without
 * touching neighbouring sections — important UX-wise because
 * Business Hours is dense and tenants will iterate on it alone.
 *
 * Each section is its own RHF instance to keep the dirty/touched
 * state local. Server-side validation is the authority; client zod
 * mirrors the Pydantic shape in tenant_settings.py for fast feedback.
 *
 * Email change opens a "Contact support" dialog rather than wiring a
 * full verification round-trip — that lands as a follow-up PR.
 */

import * as React from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm, type UseFormReturn } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  BusinessHoursGrid,
  type BusinessHoursValue,
} from "@/components/forms/BusinessHoursGrid";
import { apiFetch } from "@/lib/api/client";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import type { PlanTier } from "@/lib/tier-gate";

export const personaToneSchema = z.enum([
  "professional",
  "friendly",
  "casual",
  "formal",
]);
const timeSchema = z
  .string()
  .regex(/^([01]\d|2[0-3]):[0-5]\d$/, "Use HH:MM 24-hour format");

const businessHoursDaySchema = z
  .object({
    open: timeSchema,
    close: timeSchema,
    closed: z.boolean(),
  })
  // Mirror the server-side BusinessHoursDay validator (QA finding M6): an open
  // day's close must be strictly after its open. Closed days keep placeholder
  // times, so the rule only applies when the day is open. Zero-padded HH:MM is
  // lexicographically ordered by time, so a string compare is correct.
  .refine((d) => d.closed || d.close > d.open, {
    message: "Close time must be after open time",
    path: ["close"],
  });

const businessHoursSchema = z.object({
  mon: businessHoursDaySchema,
  tue: businessHoursDaySchema,
  wed: businessHoursDaySchema,
  thu: businessHoursDaySchema,
  fri: businessHoursDaySchema,
  sat: businessHoursDaySchema,
  sun: businessHoursDaySchema,
});

export const personaSchema = z.object({
  system_prompt: z.string().max(4000, "Max 4000 characters"),
  persona_tone: personaToneSchema,
});

// --------------------------------------------------------------------
// Persona blocks (Agent-screen redesign 2026-06-21)
// --------------------------------------------------------------------
// Mirrors the Pydantic PersonaBlock SSOT in
// packages/shared-types/python/polaris_shared_types/agent_config.py. Hand-written
// here because the portal doesn't import the generated TS (the generated file is
// gitignored/unused at GA) — keep field names byte-identical to the JSON.

export const MAX_PERSONA_BLOCKS = 10;

// Platform-owned XML tags a tenant block must never shadow/inject. Mirrors
// RESERVED_BLOCK_SLUGS in the Python SSOT. ReadonlySet so the guard can't be
// mutated by a consumer (matches the Python frozenset).
export const RESERVED_BLOCK_SLUGS: ReadonlySet<string> = new Set<string>([
  "non_negotiable_rules",
  "format",
  "email_draft",
  "using_tools",
  "runtime_language",
  "knowledge_chunk",
  "tenant_instructions",
]);

/** Derive a stable snake_case slug from a human block name. Mirrors
 * slugify_block_name in the Python SSOT, including the reserved-slug guard. */
export function slugifyBlockName(name: string): string {
  const s =
    name
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .replace(/_+/g, "_") || "block";
  return RESERVED_BLOCK_SLUGS.has(s) ? `custom_${s}` : s;
}

export const personaBlockSchema = z.object({
  display_name: z.string().min(1, "Name required").max(60, "Max 60 characters"),
  slug: z
    .string()
    .min(1)
    .max(50)
    .regex(/^[a-z0-9_]+$/, "Lowercase letters, numbers, and underscores only")
    .refine((s) => !RESERVED_BLOCK_SLUGS.has(s), "That name is reserved"),
  body: z.string().min(1, "Content required").max(2000, "Max 2000 characters"),
});

export type PersonaBlockValue = z.infer<typeof personaBlockSchema>;

export const personaBlocksSchema = z
  .array(personaBlockSchema)
  .max(MAX_PERSONA_BLOCKS, `Up to ${MAX_PERSONA_BLOCKS} blocks`)
  .superRefine((blocks, ctx) => {
    const seen = new Set<string>();
    blocks.forEach((b, i) => {
      if (seen.has(b.slug)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: `Duplicate section "${b.slug}". Rename one.`,
          path: [i, "slug"],
        });
      }
      seen.add(b.slug);
    });
  });

const handoffSchema = z.object({
  handoff_message_in_hours: z.string().max(2000),
  handoff_message_after_hours: z.string().max(2000),
});

const emailDraftSchema = z.object({
  // Matches the backend TenantSettingsUpdate.email_draft_prompt cap (4000).
  email_draft_prompt: z.string().max(4000, "Max 4000 characters"),
});

const disclosureSchema = z.object({
  ai_disclosure_enabled: z.boolean(),
});

export interface SettingsInitialValues {
  tenant_id: string;
  company_name: string;
  timezone: string;
  default_country_code: string;
  system_prompt: string;
  /** Rendered built-in persona for this tenant; used to prefill the editor. */
  default_system_prompt: string;
  /** The tenant's saved persona blocks (empty until they save the editor). */
  persona_blocks: PersonaBlockValue[];
  /** Seeded default blocks (names substituted) used to prefill the editor. */
  default_persona_blocks: PersonaBlockValue[];
  persona_tone: z.infer<typeof personaToneSchema>;
  business_hours: BusinessHoursValue;
  handoff_message_in_hours: string;
  handoff_message_after_hours: string;
  ai_disclosure_enabled: boolean;
  plan_tier: PlanTier;
  retention_message_days: number;
  /** Email channel M2: per-tenant email-draft tone prompt. Null when unset. */
  email_draft_prompt: string | null;
  user_email: string;
  agent_config: AgentConfigValue;
  agent_tool_catalog: AgentToolInfo[];
  agent_guardrails: AgentGuardrail[];
}

export interface AgentGuardrail {
  title: string;
  body: string;
}

export interface AgentConfigValue {
  enabled: boolean;
  instructions: string | null;
  enabled_tools: string[];
  max_tool_iterations: number;
}

export interface AgentToolInfo {
  id: string;
  label: string;
  description: string;
}

interface PatchPayload {
  system_prompt?: string;
  persona_tone?: z.infer<typeof personaToneSchema>;
  business_hours?: BusinessHoursValue;
  handoff_message_in_hours?: string;
  handoff_message_after_hours?: string;
  ai_disclosure_enabled?: boolean;
  email_draft_prompt?: string;
  agent_config?: AgentConfigValue;
  persona_blocks?: PersonaBlockValue[];
}

export async function patchSettings(body: PatchPayload): Promise<void> {
  try {
    await apiFetch<unknown>(TenantRoutes.settings(), {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    toast.success("Settings saved");
  } catch (err) {
    const msg =
      err instanceof PolarisApiError
        ? err.message
        : "We couldn't save your changes. Try again.";
    toast.error(msg);
    throw err;
  }
}

/**
 * Build the standard section `onSubmit` handler: patch the values, reset the
 * form to mark it clean, and swallow the rethrow from `patchFn` (which already
 * toasted) so React doesn't see a rejected promise on `<form onSubmit={...}>`
 * — that surfaces in production as an "Unhandled Promise Rejection" with no
 * actionable info.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function makePatchSubmitHandler<T extends Record<string, any>>(
  form: UseFormReturn<T>,
  patchFn: (data: T) => Promise<void>,
) {
  return form.handleSubmit(async (values) => {
    try {
      await patchFn(values);
      form.reset(values);
    } catch {
      // already toasted in patchFn; do not propagate.
    }
  });
}

// --------------------------------------------------------------------
// Section: Business profile (display-only)
// --------------------------------------------------------------------

function BusinessProfileSection({
  initial,
}: {
  initial: SettingsInitialValues;
}) {
  return (
    <SectionShell
      title="Business profile"
      description="Set at signup. Contact support if you need to change these."
    >
      <ReadonlyRow label="Company name" value={initial.company_name} />
      <ReadonlyRow label="Timezone" value={initial.timezone} />
      <ReadonlyRow
        label="Default country code"
        value={initial.default_country_code}
      />
    </SectionShell>
  );
}

// --------------------------------------------------------------------
// Section: Business hours (Mon-Sun grid)
// --------------------------------------------------------------------

function BusinessHoursSection({ initial }: { initial: SettingsInitialValues }) {
  const [value, setValue] = React.useState<BusinessHoursValue>(
    initial.business_hours,
  );
  const [dirty, setDirty] = React.useState(false);
  const [saving, setSaving] = React.useState(false);

  const onChange = (next: BusinessHoursValue) => {
    setValue(next);
    setDirty(true);
  };

  const onSave = async () => {
    setSaving(true);
    try {
      const parsed = businessHoursSchema.safeParse(value);
      if (!parsed.success) {
        toast.error(
          parsed.error.issues[0]?.message ?? "Invalid business hours",
        );
        return;
      }
      await patchSettings({ business_hours: parsed.data });
      setDirty(false);
    } catch {
      // patchSettings already toasted; swallow here so the button resets.
    } finally {
      setSaving(false);
    }
  };

  return (
    <SectionShell
      title="Business hours"
      description="Used by the bot's handoff logic to pick in-hours vs after-hours messages."
    >
      <BusinessHoursGrid value={value} onChange={onChange} disabled={saving} />
      <div className="pt-3">
        <Button type="button" onClick={onSave} disabled={!dirty || saving}>
          {saving ? "Saving…" : "Save business hours"}
        </Button>
      </div>
    </SectionShell>
  );
}

// --------------------------------------------------------------------
// Section: Handoff messages
// --------------------------------------------------------------------

function HandoffMessagesSection({
  initial,
}: {
  initial: SettingsInitialValues;
}) {
  const form = useForm<z.infer<typeof handoffSchema>>({
    resolver: zodResolver(handoffSchema),
    defaultValues: {
      handoff_message_in_hours: initial.handoff_message_in_hours,
      handoff_message_after_hours: initial.handoff_message_after_hours,
    },
  });
  const onSubmit = makePatchSubmitHandler(form, patchSettings);

  return (
    <SectionShell
      title="Handoff messages"
      description="Customer-facing text when the bot escalates to a human."
    >
      <Form {...form}>
        <form onSubmit={onSubmit} className="space-y-4">
          <FormField
            control={form.control}
            name="handoff_message_in_hours"
            render={({ field }) => (
              <FormItem>
                <FormLabel>During business hours</FormLabel>
                <FormControl>
                  <Textarea rows={3} {...field} />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
          <FormField
            control={form.control}
            name="handoff_message_after_hours"
            render={({ field }) => (
              <FormItem>
                <FormLabel>After hours</FormLabel>
                <FormControl>
                  <Textarea rows={3} {...field} />
                </FormControl>
                <FormDescription>
                  Sets expectations when no agent is available immediately.
                </FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />
          <SaveButton
            disabled={!form.formState.isDirty || form.formState.isSubmitting}
          />
        </form>
      </Form>
    </SectionShell>
  );
}

// --------------------------------------------------------------------
// Section: Email draft tone (email channel M2, Pro+)
// --------------------------------------------------------------------

function EmailDraftToneSection({
  initial,
}: {
  initial: SettingsInitialValues;
}) {
  const form = useForm<z.infer<typeof emailDraftSchema>>({
    resolver: zodResolver(emailDraftSchema),
    defaultValues: {
      // Null (unset) → empty string so the textarea is controlled.
      email_draft_prompt: initial.email_draft_prompt ?? "",
    },
  });
  const onSubmit = makePatchSubmitHandler(form, patchSettings);

  return (
    <SectionShell
      title="Email draft tone"
      description="Extra guidance for how the bot drafts email replies. Drafts post as private notes for an agent to review and send. This never changes your live email."
    >
      <Form {...form}>
        <form onSubmit={onSubmit} className="space-y-4">
          <FormField
            control={form.control}
            name="email_draft_prompt"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Draft instructions</FormLabel>
                <FormControl>
                  <Textarea
                    rows={4}
                    placeholder="e.g. Warm but concise. Always offer a callback. Sign off as the Support team."
                    {...field}
                  />
                </FormControl>
                <FormDescription>
                  Leave blank to use the default tone. Max 4000 characters.
                </FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />
          <SaveButton
            disabled={!form.formState.isDirty || form.formState.isSubmitting}
          />
        </form>
      </Form>
    </SectionShell>
  );
}

// --------------------------------------------------------------------
// Section: AI disclosure
// --------------------------------------------------------------------

function DisclosureSection({ initial }: { initial: SettingsInitialValues }) {
  const form = useForm<z.infer<typeof disclosureSchema>>({
    resolver: zodResolver(disclosureSchema),
    defaultValues: {
      ai_disclosure_enabled: initial.ai_disclosure_enabled,
    },
  });
  const value = form.watch("ai_disclosure_enabled");

  const onSubmit = makePatchSubmitHandler(form, patchSettings);

  return (
    <SectionShell
      title="AI disclosure"
      description="Whether your bot tells customers up front that they're chatting with AI."
    >
      <Form {...form}>
        <form onSubmit={onSubmit} className="space-y-4">
          <FormField
            control={form.control}
            name="ai_disclosure_enabled"
            render={({ field }) => (
              <FormItem className="flex items-center justify-between rounded-xl p-3 outline outline-1 -outline-offset-1 outline-border">
                <div className="space-y-0.5 pr-4">
                  <FormLabel>Disclose AI to customers</FormLabel>
                  <FormDescription>
                    Recommended on. Some jurisdictions require this; turning it
                    off is at your own risk.
                  </FormDescription>
                </div>
                <FormControl>
                  <Switch
                    checked={field.value}
                    onCheckedChange={field.onChange}
                  />
                </FormControl>
              </FormItem>
            )}
          />
          {!value ? (
            <p className="rounded-xl bg-warning/10 px-3 py-2 text-xs text-warning outline outline-1 -outline-offset-1 outline-warning">
              Disclosure is off. Verify this complies with your local
              consumer-protection rules.
            </p>
          ) : null}
          <SaveButton
            disabled={!form.formState.isDirty || form.formState.isSubmitting}
          />
        </form>
      </Form>
    </SectionShell>
  );
}

// --------------------------------------------------------------------
// Section: Account (email + deletion CTA)
// --------------------------------------------------------------------

function AccountSection({
  initial,
  onOpenEmailChange,
  onOpenDeletion,
}: {
  initial: SettingsInitialValues;
  onOpenEmailChange: () => void;
  onOpenDeletion: () => void;
}) {
  return (
    <SectionShell
      title="Account"
      description="Sign-in email and account deletion."
    >
      <div className="space-y-3">
        <div className="flex items-center justify-between rounded-xl p-3 outline outline-1 -outline-offset-1 outline-border">
          <div>
            <p className="text-sm font-medium">Sign-in email</p>
            <p className="text-xs text-muted-foreground">
              {initial.user_email}
            </p>
          </div>
          <Button type="button" variant="outline" onClick={onOpenEmailChange}>
            Change email
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          Need to give a coworker access? Contact support. Additional logins
          coming in v2.1.
        </p>
        <div className="flex items-center justify-between rounded-xl p-3 outline outline-1 -outline-offset-1 outline-border">
          <div>
            <p className="text-sm font-medium">Export my data</p>
            <p className="text-xs text-muted-foreground">
              Download a copy of your tenant data (DSAR).
            </p>
          </div>
          <Button asChild type="button" variant="outline">
            <a href="/settings/dsar/export">Export data</a>
          </Button>
        </div>
        <div className="flex items-center justify-between rounded-xl p-3 outline outline-1 -outline-offset-1 outline-destructive/40">
          <div>
            <p className="text-sm font-medium text-destructive">
              Delete account
            </p>
            <p className="text-xs text-muted-foreground">
              30-day recovery window after confirmation.
            </p>
          </div>
          <Button type="button" variant="destructive" onClick={onOpenDeletion}>
            Request deletion
          </Button>
        </div>
        <div className="rounded-xl bg-muted/40 p-3 text-xs text-muted-foreground outline outline-1 -outline-offset-1 outline-border">
          Need a second agent seat? Email{" "}
          <a
            href="mailto:billing@getpolaris.my?subject=Add%20agent%20seat"
            className="underline"
          >
            billing@getpolaris.my
          </a>
          . Add-on: RM 100/month per seat.
        </div>
      </div>
    </SectionShell>
  );
}

// --------------------------------------------------------------------
// Section: Compliance (display-only)
// --------------------------------------------------------------------

function ComplianceSection({ initial }: { initial: SettingsInitialValues }) {
  return (
    <SectionShell
      title="Compliance"
      description="Data residency and retention. Set by your plan tier."
    >
      <ReadonlyRow label="Data residency" value="Singapore (ap-southeast-1)" />
      <ReadonlyRow
        label="Message retention"
        value={`${initial.retention_message_days} days`}
      />
      <div className="pt-2">
        <Button asChild variant="outline" type="button">
          <a href="/settings/dsar/export">Export my data (DSAR)</a>
        </Button>
      </div>
    </SectionShell>
  );
}

// --------------------------------------------------------------------
// Shared building blocks
// --------------------------------------------------------------------

export function SectionShell({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl bg-card p-6 outline outline-1 -outline-offset-1 outline-border">
      <header className="mb-4">
        <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
        <p className="mt-1 text-sm text-muted-foreground">{description}</p>
      </header>
      {children}
    </section>
  );
}

function ReadonlyRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between rounded-xl bg-background/60 p-3 outline outline-1 -outline-offset-1 outline-border">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="text-sm font-medium">{value}</span>
    </div>
  );
}

export function SaveButton({ disabled }: { disabled?: boolean }) {
  return (
    <div>
      <Button type="submit" disabled={disabled}>
        Save changes
      </Button>
    </div>
  );
}

// --------------------------------------------------------------------
// Composite
// --------------------------------------------------------------------

export function SettingsForm({
  initial,
  onOpenEmailChange,
  onOpenDeletion,
}: {
  initial: SettingsInitialValues;
  onOpenEmailChange: () => void;
  onOpenDeletion: () => void;
}) {
  return (
    <div className="space-y-5">
      <BusinessProfileSection initial={initial} />
      <BusinessHoursSection initial={initial} />
      <HandoffMessagesSection initial={initial} />
      {/* Email channel is Pro+; hide the tone editor for Starter tenants. */}
      {initial.plan_tier !== "starter" && (
        <EmailDraftToneSection initial={initial} />
      )}
      <DisclosureSection initial={initial} />
      <AccountSection
        initial={initial}
        onOpenEmailChange={onOpenEmailChange}
        onOpenDeletion={onOpenDeletion}
      />
      <ComplianceSection initial={initial} />
    </div>
  );
}
