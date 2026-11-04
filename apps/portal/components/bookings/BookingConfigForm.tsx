"use client";

/**
 * Booking config screen (Phase 8 PR-8 + EA-mirror follow-on).
 *
 * Two panels:
 *   1. **Calendar settings** — READ ONLY. Mirrors the tenant's Easy!Appointments
 *      (Calendar admin) config: cancellation/reschedule lead-time, working hours
 *      + breaks, blocked dates, future-booking limit, timezone. EA is the source
 *      of truth; this panel shows "last synced / next refresh in mm:ss" and an
 *      "Update bot knowledge" button that pulls the latest EA values into the
 *      bot now (`POST /api/tenant/bookings/config/sync`).
 *   2. **Bot messaging** — EDITABLE. The Polaris-only fields EA has no home for
 *      (confirmation message, self-serve cancel). Saved via `PUT .../config`;
 *      EA-owned fields in the body are ignored server-side.
 *
 * The lead-time is a SINGLE value (EA's `book_advance_timeout`) — the old two
 * editable lead-time fields collapsed onto it.
 */

import * as React from "react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { apiFetch } from "@/lib/api/client";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";

// The tenant's Easy!Appointments admin lives at the product's calendar domain.
const CALENDAR_ADMIN_URL = "https://calendar.getpolaris.my";

export interface DayPlan {
  start: string;
  end: string;
  breaks?: { start: string; end: string }[];
}

export interface BlockedPeriod {
  start: string | null;
  end: string | null;
  notes: string;
  name: string;
}

export interface EaCalendarDTO {
  lead_time_hours: number | null;
  timezone: string | null;
  working_plan: Record<string, DayPlan | null> | null;
  blocked_periods: BlockedPeriod[];
  future_booking_limit_days: number | null;
  synced_at: string | null;
  next_refresh_at: string | null;
  refresh_interval_s: number | null;
}

export interface BookingConfigDTO {
  cancellation_lead_hours: number;
  reschedule_window_hours: number;
  confirmation_message: string;
  allow_self_serve_cancellation: boolean;
  /** Phase 2 in-chat slot picker. When true, the bot offers available times
   *  in the chat instead of sending a booking link. Off by default. */
  inchat_picker: boolean;
  // Bot-only fields not surfaced as inputs here, but carried through on save so
  // they're never silently reset to defaults.
  reminder_offset_hours?: number;
  quiet_hours_start?: string | null;
  quiet_hours_end?: string | null;
  tenant_timezone?: string | null;
  confirmation_template_name?: string | null;
  confirmation_template_language?: string | null;
  reminder_template_name?: string | null;
  reminder_template_language?: string | null;
  cancellation_template_name?: string | null;
  cancellation_template_language?: string | null;
  reschedule_template_name?: string | null;
  reschedule_template_language?: string | null;
}

export interface BookingConfigView extends BookingConfigDTO {
  ea_calendar: EaCalendarDTO | null;
}

const _DAY_ORDER = [
  "monday",
  "tuesday",
  "wednesday",
  "thursday",
  "friday",
  "saturday",
  "sunday",
] as const;

function _dayLabel(day: string): string {
  return day.charAt(0).toUpperCase() + day.slice(1, 3);
}

/** "2026-12-25 00:00:00" → "2026-12-25"; passthrough for already-short values. */
function _dateOnly(value: string | null): string {
  if (!value) return "—";
  return value.split(" ")[0]?.split("T")[0] ?? value;
}

function _splitView(view: BookingConfigView): {
  config: BookingConfigDTO;
  eaCalendar: EaCalendarDTO | null;
} {
  const { ea_calendar, ...config } = view;
  return { config, eaCalendar: ea_calendar };
}

/** Live mm:ss countdown to `target`; "refreshing…" once it elapses. */
function Countdown({ target }: { target: string | null }) {
  const [remaining, setRemaining] = React.useState<number | null>(null);

  React.useEffect(() => {
    if (!target) {
      setRemaining(null);
      return;
    }
    const targetMs = new Date(target).getTime();
    if (Number.isNaN(targetMs)) {
      setRemaining(null);
      return;
    }
    const tick = () =>
      setRemaining(Math.max(0, Math.round((targetMs - Date.now()) / 1000)));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [target]);

  if (remaining === null) return null;
  if (remaining <= 0) return <span>refreshing soon…</span>;
  const mm = String(Math.floor(remaining / 60)).padStart(2, "0");
  const ss = String(remaining % 60).padStart(2, "0");
  return (
    <span>
      next auto-refresh in {mm}:{ss}
    </span>
  );
}

function _relativeSince(iso: string | null): string {
  if (!iso) return "never";
  const seconds = Math.max(
    0,
    Math.round((Date.now() - new Date(iso).getTime()) / 1000),
  );
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function EaCalendarPanel({
  eaCalendar,
  onSync,
  syncing,
}: {
  eaCalendar: EaCalendarDTO | null;
  onSync: () => void;
  syncing: boolean;
}) {
  return (
    <section className="space-y-4 rounded-xl border bg-muted/20 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Calendar settings</h2>
          <p className="text-xs text-muted-foreground">
            Read-only. The source of truth is your Calendar admin app. Edit
            hours, breaks, blocked dates, and lead-time there.{" "}
            <a
              className="underline"
              href={CALENDAR_ADMIN_URL}
              target="_blank"
              rel="noopener noreferrer"
            >
              Open Calendar admin →
            </a>
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onSync}
          disabled={syncing}
        >
          {syncing ? "Updating…" : "Update bot knowledge"}
        </Button>
      </div>

      {eaCalendar === null ? (
        <p className="text-sm text-muted-foreground">
          Not synced yet. Click <strong>Update bot knowledge</strong> to pull
          your calendar settings into the bot.
        </p>
      ) : (
        <dl className="grid gap-4 sm:grid-cols-2">
          <div>
            <dt className="text-xs font-medium text-muted-foreground">
              Cancellation &amp; reschedule lead-time
            </dt>
            <dd className="text-sm">
              {eaCalendar.lead_time_hours !== null
                ? `${eaCalendar.lead_time_hours}h before the appointment`
                : "Not configured"}
            </dd>
          </div>
          <div>
            <dt className="text-xs font-medium text-muted-foreground">
              Future booking limit
            </dt>
            <dd className="text-sm">
              {eaCalendar.future_booking_limit_days !== null
                ? `${eaCalendar.future_booking_limit_days} days`
                : "Not configured"}
            </dd>
          </div>
          <div>
            <dt className="text-xs font-medium text-muted-foreground">
              Timezone
            </dt>
            <dd className="text-sm">
              {eaCalendar.timezone ?? "Not configured"}
            </dd>
          </div>
          <div>
            <dt className="text-xs font-medium text-muted-foreground">
              Blocked dates
            </dt>
            <dd className="text-sm">
              {eaCalendar.blocked_periods.length === 0
                ? "None"
                : eaCalendar.blocked_periods
                    .map(
                      (b) =>
                        _dateOnly(b.start) + (b.name ? ` (${b.name})` : ""),
                    )
                    .join(", ")}
            </dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-xs font-medium text-muted-foreground">
              Working hours
            </dt>
            <dd className="mt-1 space-y-0.5 text-sm">
              {eaCalendar.working_plan === null
                ? "Not configured"
                : _DAY_ORDER.map((day) => {
                    const plan = eaCalendar.working_plan?.[day] ?? null;
                    const breaks =
                      plan?.breaks
                        ?.map((br) => `${br.start}–${br.end}`)
                        .join(", ") ?? "";
                    return (
                      <div key={day} className="flex gap-2">
                        <span className="w-10 shrink-0 text-muted-foreground">
                          {_dayLabel(day)}
                        </span>
                        <span>
                          {plan
                            ? `${plan.start}–${plan.end}${
                                breaks ? ` · break ${breaks}` : ""
                              }`
                            : "Closed"}
                        </span>
                      </div>
                    );
                  })}
            </dd>
          </div>
        </dl>
      )}

      {eaCalendar !== null ? (
        <p className="text-xs text-muted-foreground">
          Last synced {_relativeSince(eaCalendar.synced_at)} ·{" "}
          <Countdown target={eaCalendar.next_refresh_at} />
        </p>
      ) : null}
    </section>
  );
}

export function BookingConfigForm({ initial }: { initial: BookingConfigView }) {
  const split = _splitView(initial);
  const [config, setConfig] = React.useState<BookingConfigDTO>(split.config);
  const [eaCalendar, setEaCalendar] = React.useState<EaCalendarDTO | null>(
    split.eaCalendar,
  );
  const [saving, setSaving] = React.useState(false);
  const [syncing, setSyncing] = React.useState(false);
  const [savedAt, setSavedAt] = React.useState<number | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [syncError, setSyncError] = React.useState<string | null>(null);

  function set<K extends keyof BookingConfigDTO>(
    key: K,
    value: BookingConfigDTO[K],
  ) {
    setConfig((cur) => ({ ...cur, [key]: value }));
    setSavedAt(null);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      // Send the full config (incl. carried-through bot-only fields) so nothing
      // is reset; the server ignores EA-owned fields and echoes the saved view.
      const saved = await apiFetch<BookingConfigView>(
        TenantRoutes.bookingsConfig(),
        { method: "PUT", body: JSON.stringify(config) },
      );
      if (saved) {
        const next = _splitView(saved);
        setConfig(next.config);
        setEaCalendar(next.eaCalendar);
      }
      setSavedAt(Date.now());
    } catch (err) {
      setError(
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't save. Try again.",
      );
    } finally {
      setSaving(false);
    }
  }

  async function sync() {
    setSyncing(true);
    setSyncError(null);
    try {
      const fresh = await apiFetch<BookingConfigView>(
        TenantRoutes.bookingsConfigSync(),
        { method: "POST" },
      );
      if (fresh) {
        const next = _splitView(fresh);
        setConfig(next.config);
        setEaCalendar(next.eaCalendar);
      }
    } catch (err) {
      setSyncError(
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't refresh from the calendar. Try again.",
      );
    } finally {
      setSyncing(false);
    }
  }

  return (
    <div className="space-y-6">
      <EaCalendarPanel
        eaCalendar={eaCalendar}
        onSync={sync}
        syncing={syncing}
      />
      {syncError ? (
        <p role="alert" className="text-sm text-destructive">
          {syncError}
        </p>
      ) : null}

      <form onSubmit={submit} className="space-y-5 rounded-xl border p-5">
        <div>
          <h2 className="text-sm font-semibold">Bot messaging</h2>
          <p className="text-xs text-muted-foreground">
            How the bot talks to customers after a booking. These are
            Polaris-only and editable here.
          </p>
        </div>

        <div className="space-y-2">
          <Label htmlFor="confirm-msg">Confirmation message</Label>
          <Textarea
            id="confirm-msg"
            rows={5}
            value={config.confirmation_message}
            onChange={(e) => set("confirmation_message", e.target.value)}
            placeholder="Your booking is confirmed for {{date}} at {{time}}. See you then!"
          />
          <p className="text-xs text-muted-foreground">
            The text the bot sends once an appointment is confirmed. Variables (
            {"{{date}}, {{time}}, {{service}}, {{provider}}"}) get filled
            automatically. Leave empty to use the default per-service template.
          </p>
        </div>

        <div className="flex items-center gap-2 rounded-md border bg-muted/20 p-3">
          <Switch
            id="self-serve"
            checked={config.allow_self_serve_cancellation}
            onCheckedChange={(checked) =>
              set("allow_self_serve_cancellation", checked)
            }
          />
          <Label htmlFor="self-serve" className="text-sm font-normal">
            Allow customers to self-serve cancel via the bot (inside the
            lead-time window).
          </Label>
        </div>

        <div className="flex items-start gap-2 rounded-md border bg-muted/20 p-3">
          <Switch
            id="inchat-picker"
            checked={config.inchat_picker ?? false}
            onCheckedChange={(checked) => set("inchat_picker", checked)}
          />
          <div>
            <Label htmlFor="inchat-picker" className="text-sm font-normal">
              Let customers pick a time in chat
            </Label>
            <p className="text-xs text-muted-foreground">
              Off = send a booking link · On = offer available times in the chat
            </p>
          </div>
        </div>

        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : null}

        <div className="flex items-center gap-3">
          <Button type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
          {savedAt !== null ? (
            <span className="text-xs text-emerald-700 dark:text-emerald-400">
              Saved.
            </span>
          ) : null}
        </div>
      </form>
    </div>
  );
}
