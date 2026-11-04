/**
 * Shared server-side loader for the tenant settings envelope.
 *
 * Both the Settings screen (#15) and the AI Agent screen read the same
 * `GET /api/tenant/settings` payload, so the fetch + defensive-fallback
 * shape lives here once. Server-only (uses `apiFetchServer`).
 */

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import type {
  AgentGuardrail,
  PersonaBlockValue,
  SettingsInitialValues,
} from "@/components/settings/SettingsForm";
import type { BusinessHoursValue } from "@/components/forms/BusinessHoursGrid";
import type { PlanTier } from "@/lib/tier-gate";

const VALID_TIERS: ReadonlySet<PlanTier> = new Set([
  "starter",
  "pro",
  "ultra",
  "enterprise",
]);

function narrowTier(raw: string): PlanTier {
  return VALID_TIERS.has(raw as PlanTier) ? (raw as PlanTier) : "starter";
}

interface SettingsApiResponse {
  tenant_id: string;
  company_name: string;
  timezone: string;
  default_country_code: string;
  system_prompt: string;
  default_system_prompt: string;
  persona_blocks: PersonaBlockValue[];
  default_persona_blocks: PersonaBlockValue[];
  persona_tone: "professional" | "friendly" | "casual" | "formal";
  business_hours: BusinessHoursValue;
  handoff_message_in_hours: string;
  handoff_message_after_hours: string;
  ai_disclosure_enabled: boolean;
  plan_tier: string;
  retention_message_days: number;
  /** Email channel M2: per-tenant email-draft tone prompt. Null when unset. */
  email_draft_prompt: string | null;
  agent_config: {
    enabled: boolean;
    instructions: string | null;
    enabled_tools: string[];
    max_tool_iterations: number;
  };
  agent_tool_catalog: { id: string; label: string; description: string }[];
  agent_guardrails: AgentGuardrail[];
}

const FALLBACK_HOURS: BusinessHoursValue = {
  mon: { open: "09:00", close: "18:00", closed: false },
  tue: { open: "09:00", close: "18:00", closed: false },
  wed: { open: "09:00", close: "18:00", closed: false },
  thu: { open: "09:00", close: "18:00", closed: false },
  fri: { open: "09:00", close: "18:00", closed: false },
  sat: { open: "09:00", close: "18:00", closed: true },
  sun: { open: "09:00", close: "18:00", closed: true },
};

export async function loadSettings(
  userEmail: string,
): Promise<SettingsInitialValues> {
  let settings: SettingsApiResponse | undefined;
  try {
    settings = await apiFetchServer<SettingsApiResponse>(
      TenantRoutes.settings(),
    );
  } catch (err) {
    const code =
      err instanceof PolarisApiError
        ? `${err.status} ${err.code}`
        : String(err);
    // eslint-disable-next-line no-console
    console.error(`[portal settings] fetch failed: ${code}`, err);
  }
  if (!settings) {
    // Defensive shape — the page still renders so the tenant has a recovery
    // path (sign out + retry) rather than a blank 500.
    return {
      tenant_id: "",
      company_name: "Your business",
      timezone: "Asia/Kuala_Lumpur",
      default_country_code: "MY",
      system_prompt: "",
      default_system_prompt: "",
      persona_blocks: [],
      default_persona_blocks: [],
      persona_tone: "professional",
      business_hours: FALLBACK_HOURS,
      handoff_message_in_hours:
        "Connecting you with a teammate. They'll be with you shortly.",
      handoff_message_after_hours:
        "Our team is offline. They'll reach out first thing on the next business day.",
      ai_disclosure_enabled: true,
      plan_tier: "starter",
      retention_message_days: 90,
      email_draft_prompt: null,
      user_email: userEmail,
      agent_config: {
        enabled: false,
        instructions: null,
        enabled_tools: [],
        max_tool_iterations: 5,
      },
      agent_tool_catalog: [],
      agent_guardrails: [],
    };
  }
  return {
    ...settings,
    plan_tier: narrowTier(settings.plan_tier),
    user_email: userEmail,
  };
}
