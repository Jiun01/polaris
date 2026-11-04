/**
 * Bookings — config screen #9 (Phase 8 PR-8 + EA-mirror follow-on).
 *
 * Server Component shell fetches the current booking config + EA mirror from
 * `/api/tenant/bookings/config` and hands it to a client form. The form renders
 * two panels: a READ-ONLY "Calendar settings" mirror of Easy!Appointments (the
 * source of truth) with an "Update bot knowledge" sync button + auto-refresh
 * countdown, and an EDITABLE "Bot messaging" panel for the Polaris-only fields.
 *
 * Services, staff, hours, and blocked dates are self-managed in the Calendar
 * admin app — not by us — so the copy below points the tenant there.
 */

import { apiFetchServer } from "@/lib/api/server";
import { TenantRoutes } from "@/lib/api/routes";
import {
  BookingConfigForm,
  type BookingConfigView,
} from "@/components/bookings/BookingConfigForm";

const DEFAULT_CONFIG: BookingConfigView = {
  cancellation_lead_hours: 24,
  reschedule_window_hours: 24,
  confirmation_message: "",
  allow_self_serve_cancellation: true,
  inchat_picker: false,
  ea_calendar: null,
};

async function loadConfig(): Promise<{
  config: BookingConfigView;
  error?: string;
}> {
  try {
    const res = await apiFetchServer<BookingConfigView>(
      TenantRoutes.bookingsConfig(),
    );
    return { config: res ?? DEFAULT_CONFIG };
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error("[portal /bookings] config fetch failed", err);
    return {
      config: DEFAULT_CONFIG,
      error: "Couldn't load booking config. Showing defaults.",
    };
  }
}

export default async function BookingsConfigPage() {
  const { config, error } = await loadConfig();

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold tracking-tight">Bookings</h1>
        <p className="text-sm text-muted-foreground">
          Your calendar settings (hours, lead-time, blocked dates) come from the
          Calendar admin app and are mirrored to the bot below. Tune how the bot
          talks to customers in the Bot messaging panel.
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

      <BookingConfigForm initial={config} />

      <section className="rounded-xl border bg-muted/30 p-5">
        <h2 className="text-sm font-semibold">
          Where do I manage services, staff, and hours?
        </h2>
        <p className="mt-2 text-sm text-muted-foreground">
          Services, providers, working hours, breaks, and blocked dates are
          managed by you in the Polaris Calendar admin app. It&apos;s the source
          of truth, and the bot reads from it.
        </p>
        <p className="mt-2 text-sm text-muted-foreground">
          After changing anything in Calendar admin, click{" "}
          <strong>Update bot knowledge</strong> above to push the new settings
          to the bot immediately. Otherwise they refresh automatically on the
          next cycle.
        </p>
      </section>
    </div>
  );
}
