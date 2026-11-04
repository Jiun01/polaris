/**
 * Website Widget screen (off-Meta channels M4).
 *
 * Server Component. Fetches the tenant's launcher-widget config from
 * /api/tenant/widget (lazily provisions a widget_key server-side) and renders
 * the self-serve config form via <WidgetClient>.
 *
 * The widget is a click-to-chat LAUNCHER, not a hosted chat: it routes site
 * visitors INTO the tenant's real channels (Telegram / WhatsApp / ...). Editing
 * here changes the live bubble without re-embedding the snippet.
 */

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import {
  WidgetClient,
  type WidgetConfig,
} from "@/components/widget/WidgetClient";

async function loadConfig(): Promise<{
  config?: WidgetConfig;
  error?: string;
}> {
  try {
    const res = await apiFetchServer<WidgetConfig>(TenantRoutes.widget());
    if (res === undefined) {
      return { error: "We're having trouble loading your widget settings." };
    }
    return { config: res };
  } catch (err) {
    const code =
      err instanceof PolarisApiError
        ? `${err.status} ${err.code}`
        : String(err);
    // eslint-disable-next-line no-console
    console.error(`[portal /widget] fetch failed: ${code}`, err);
    return { error: "We're having trouble loading your widget settings." };
  }
}

export default async function WidgetPage() {
  const { config, error } = await loadConfig();

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight">
          Website Widget
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          A floating &ldquo;chat with us&rdquo; button for your website that
          sends visitors straight into your Telegram, WhatsApp, and other
          channels.
        </p>
      </header>

      {error || !config ? (
        <div
          role="status"
          className="rounded-lg border border-warning bg-warning/10 px-4 py-3 text-sm text-warning"
        >
          {error ?? "Widget settings unavailable."}
        </div>
      ) : (
        <WidgetClient initial={config} />
      )}
    </div>
  );
}
