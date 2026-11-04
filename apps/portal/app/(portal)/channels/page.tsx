/**
 * Channels screen #3 (Phase 8 PR-5).
 *
 * Server Component. Fetches the channel envelope from
 * /api/tenant/channels (read-only) and renders the status tiles
 * via <ChannelsClient>.
 *
 * Channels connect automatically once set up, so this screen is a
 * read-only status view: no self-serve paste or contact-support flow.
 */

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { ChannelsClient } from "@/components/channels/ChannelsClient";
import { type ChannelKind as CanonicalChannelKind } from "@/lib/channel-kinds";

// Legacy "instagram" included: /api/tenant/channels reads tenant_channels
// rows directly, and rows provisioned before the Instagram Login migration
// can still carry the legacy FB-page kind. See plan Decision #2.
type ChannelKind = CanonicalChannelKind | "instagram";

interface ChannelsApiResponse {
  items: Array<{
    id: string;
    channel: ChannelKind;
    status: string;
    chatwoot_inbox_id: number;
    waba_id: string | null;
    phone_number_id: string | null;
    fb_page_id: string | null;
    ig_business_account_id: string | null;
    wa_messaging_tier: string | null;
    mm_lite_status: string | null;
    telegram_bot_username: string | null;
    created_at: string;
  }>;
  per_kind: Record<ChannelKind, number>;
}

const EMPTY_PER_KIND: Record<ChannelKind, number> = {
  whatsapp: 0,
  instagram: 0,
  instagram_login: 0,
  messenger: 0,
  telegram: 0,
  email: 0,
};

async function loadChannels(): Promise<{
  perKind: Record<ChannelKind, number>;
  error?: string;
}> {
  try {
    const res = await apiFetchServer<ChannelsApiResponse>(
      TenantRoutes.channels(),
    );
    if (res === undefined) {
      return { perKind: EMPTY_PER_KIND };
    }
    // Server returns all 6 ChannelKind keys initialised to 0 (dict.fromkeys
    // over get_args(ChannelKind) in tenant_channels.py) — spread here is
    // just belt-and-suspenders in case that ever changes.
    return {
      perKind: { ...EMPTY_PER_KIND, ...res.per_kind },
    };
  } catch (err) {
    const code =
      err instanceof PolarisApiError
        ? `${err.status} ${err.code}`
        : String(err);
    // eslint-disable-next-line no-console
    console.error(`[portal /channels] fetch failed: ${code}`, err);
    return {
      perKind: EMPTY_PER_KIND,
      error: "We're having trouble loading your channels.",
    };
  }
}

export default async function ChannelsPage() {
  const { perKind, error } = await loadChannels();

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight">Channels</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          See the status of your WhatsApp, Instagram, and Messenger channels.
          Manage agent inboxes in{" "}
          <a
            className="underline"
            href="https://chat.getpolaris.my"
            target="_blank"
            rel="noreferrer"
          >
            your messaging inbox
          </a>
          .
        </p>
      </header>

      {error ? (
        <div
          role="status"
          className="rounded-lg border border-warning bg-warning/10 px-4 py-3 text-sm text-warning"
        >
          {error}
        </div>
      ) : null}

      <ChannelsClient perKind={perKind} />
    </div>
  );
}
