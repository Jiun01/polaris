/**
 * New template — compose + submit (Phase 8 PR-8).
 *
 * Server Component shell hands off to a Client Component for the
 * actual form — Meta-submission requires a few stateful interactions
 * (preview + variable detection in body) that don't fit a pure SC.
 *
 * Channel binding: the form needs the tenant's WhatsApp tenant_channel
 * id (the inbox the template will submit under). At GA tenants
 * typically have one WhatsApp channel; the picker is hidden when
 * exactly one exists.
 */

import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { TemplateForm } from "@/components/templates/TemplateForm";

interface ChannelDTO {
  id: string;
  channel: "whatsapp" | "instagram" | "messenger";
  status: string;
}

interface ChannelsApiResponse {
  items: ChannelDTO[];
  per_kind: Record<string, number>;
}

async function loadWhatsAppChannels(): Promise<ChannelDTO[]> {
  try {
    const res = await apiFetchServer<ChannelsApiResponse>(
      TenantRoutes.channels(),
    );
    return (res?.items ?? []).filter((c) => c.channel === "whatsapp");
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error("[portal /templates/new] channels fetch failed", err);
    if (err instanceof PolarisApiError) throw err;
    return [];
  }
}

export default async function NewTemplatePage() {
  const whatsappChannels = await loadWhatsAppChannels();

  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/templates"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
          All templates
        </Link>
      </div>

      <header className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">New template</h1>
        <p className="text-sm text-muted-foreground">
          Templates are pre-approved messages you can send to opted-in customers
          via WhatsApp. Meta reviews each submission within 24 hours.
        </p>
      </header>

      {whatsappChannels.length === 0 ? (
        <div className="rounded-xl border border-dashed bg-muted/30 p-8 text-center">
          <h2 className="text-base font-semibold">
            Connect WhatsApp before submitting templates
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Templates submit under a specific WhatsApp business inbox. Connect
            one from the Channels screen first.
          </p>
          <p className="mt-4 text-sm">
            <Link className="underline" href="/channels">
              Go to Channels →
            </Link>
          </p>
        </div>
      ) : (
        <TemplateForm channels={whatsappChannels} />
      )}
    </div>
  );
}
