/**
 * Broadcast composer — 3-step wizard (Phase 8 PR-9).
 *
 * Server Component fetches the inputs the wizard needs in parallel:
 * approved templates + the tenant's WhatsApp channel(s). The client
 * wizard handles the multi-step flow.
 *
 * Per phase-checklist PR-9: template → audience filter → schedule.
 * Audience filter at GA is channel + tag set (server-side filter
 * already exists on /api/tenant/broadcasts/ create payload).
 *
 * Channel binding: POST /api/tenant/broadcasts/ requires a
 * `tenant_channel_id` (CreateBroadcastRequest, packages/shared-types/
 * python/polaris_shared_types/broadcast.py) — the inbox the broadcast
 * sends through. Templates are WhatsApp-only (tenant_templates.py's
 * TEMPLATE_CHANNEL_NOT_WHATSAPP validation), so this fetches the
 * tenant's WhatsApp channel(s) the same way templates/new/page.tsx does
 * (X1 fix, audit-batch-f-frontend-consolidation 2026-07 — the wizard
 * previously never fetched or sent this required field at all).
 */

import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { BroadcastWizard } from "@/components/broadcasts/BroadcastWizard";

interface TemplateRow {
  id: string;
  name: string;
  language: string;
  status: string;
}

interface ChannelDTO {
  id: string;
  channel: "whatsapp" | "instagram" | "messenger";
  status: string;
}

interface ChannelsApiResponse {
  items: ChannelDTO[];
  per_kind: Record<string, number>;
}

async function loadApprovedTemplates(): Promise<TemplateRow[]> {
  try {
    const res = await apiFetchServer<TemplateRow[]>(
      `${TenantRoutes.templates()}?status_filter=approved`,
    );
    return res ?? [];
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error("[portal /broadcasts/new] templates fetch failed", err);
    return [];
  }
}

async function loadWhatsAppChannels(): Promise<ChannelDTO[]> {
  try {
    const res = await apiFetchServer<ChannelsApiResponse>(
      TenantRoutes.channels(),
    );
    return (res?.items ?? []).filter((c) => c.channel === "whatsapp");
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error("[portal /broadcasts/new] channels fetch failed", err);
    if (err instanceof PolarisApiError) throw err;
    return [];
  }
}

export default async function NewBroadcastPage() {
  const [templates, whatsappChannels] = await Promise.all([
    loadApprovedTemplates(),
    loadWhatsAppChannels(),
  ]);

  const canCompose = templates.length > 0 && whatsappChannels.length > 0;

  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/broadcasts"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
          All broadcasts
        </Link>
      </div>

      <header className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">New broadcast</h1>
        <p className="text-sm text-muted-foreground">
          Pick an approved template, filter your audience, and schedule the
          send.
        </p>
      </header>

      {templates.length === 0 ? (
        <div className="rounded-xl border border-dashed bg-muted/30 p-8 text-center">
          <h2 className="text-base font-semibold">No approved templates yet</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Broadcasts send via Meta-approved templates. Submit one from the
            Templates screen and wait for approval (typically &lt; 24h).
          </p>
          <p className="mt-4 text-sm">
            <Link className="underline" href="/templates/new">
              Submit a template →
            </Link>
          </p>
        </div>
      ) : whatsappChannels.length === 0 ? (
        <div className="rounded-xl border border-dashed bg-muted/30 p-8 text-center">
          <h2 className="text-base font-semibold">
            Connect WhatsApp before sending broadcasts
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Broadcasts send under a specific WhatsApp business inbox. Connect
            one from the Channels screen first.
          </p>
          <p className="mt-4 text-sm">
            <Link className="underline" href="/channels">
              Go to Channels →
            </Link>
          </p>
        </div>
      ) : canCompose ? (
        <BroadcastWizard templates={templates} channels={whatsappChannels} />
      ) : null}
    </div>
  );
}
