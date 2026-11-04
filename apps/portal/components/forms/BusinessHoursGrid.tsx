"use client";

/**
 * Weekly business-hours editor (Phase 8 PR-4).
 *
 * Mon-Sun rows with open/close time inputs + a "Closed" toggle per day.
 * Used by the Settings screen (tenant-wide hours) and reused later in
 * PR-9 for `tenant_branches.business_hours` per-branch overrides.
 *
 * Shape mirrors `BusinessHours` in tenant_settings.py — handler emits
 * the value as a controlled value, not a form-field, so the parent
 * RHF form can wire it into a top-level `business_hours` field via
 * `<Controller>`.
 */

import * as React from "react";

import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

const DAYS = [
  { key: "mon", label: "Monday" },
  { key: "tue", label: "Tuesday" },
  { key: "wed", label: "Wednesday" },
  { key: "thu", label: "Thursday" },
  { key: "fri", label: "Friday" },
  { key: "sat", label: "Saturday" },
  { key: "sun", label: "Sunday" },
] as const;

export type DayKey = (typeof DAYS)[number]["key"];

export interface BusinessHoursDayValue {
  open: string;
  close: string;
  closed: boolean;
}

export type BusinessHoursValue = Record<DayKey, BusinessHoursDayValue>;

interface BusinessHoursGridProps {
  value: BusinessHoursValue;
  onChange: (next: BusinessHoursValue) => void;
  disabled?: boolean;
  className?: string;
}

export function BusinessHoursGrid({
  value,
  onChange,
  disabled,
  className,
}: BusinessHoursGridProps) {
  const updateDay = React.useCallback(
    (day: DayKey, patch: Partial<BusinessHoursDayValue>) => {
      onChange({ ...value, [day]: { ...value[day], ...patch } });
    },
    [value, onChange],
  );

  return (
    <div
      className={cn(
        "space-y-2 rounded-xl bg-background p-3 outline outline-1 -outline-offset-1 outline-border",
        className,
      )}
    >
      <div
        className="grid grid-cols-[110px_1fr_1fr_auto] items-center gap-3 text-[11px] uppercase tracking-wide text-muted-foreground"
        aria-hidden
      >
        <span>Day</span>
        <span>Opens</span>
        <span>Closes</span>
        <span>Closed</span>
      </div>
      {DAYS.map(({ key, label }) => {
        const day = value[key];
        return (
          <div
            key={key}
            className="grid grid-cols-[110px_1fr_1fr_auto] items-center gap-3"
          >
            <label
              htmlFor={`bh-${key}-open`}
              className={cn(
                "text-sm font-medium",
                day.closed && "text-muted-foreground",
              )}
            >
              {label}
            </label>
            <input
              id={`bh-${key}-open`}
              type="time"
              value={day.open}
              disabled={disabled || day.closed}
              onChange={(e) => updateDay(key, { open: e.target.value })}
              className={cn(
                "h-9 rounded-lg bg-background px-2 text-sm outline outline-1 outline-input transition-colors hover:outline-n-slate-6",
                "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                "disabled:cursor-not-allowed disabled:opacity-50",
              )}
            />
            <input
              type="time"
              aria-label={`${label} closing time`}
              value={day.close}
              disabled={disabled || day.closed}
              onChange={(e) => updateDay(key, { close: e.target.value })}
              className={cn(
                "h-9 rounded-lg bg-background px-2 text-sm outline outline-1 outline-input transition-colors hover:outline-n-slate-6",
                "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                "disabled:cursor-not-allowed disabled:opacity-50",
              )}
            />
            <Switch
              checked={day.closed}
              disabled={disabled}
              onCheckedChange={(checked) => updateDay(key, { closed: checked })}
              aria-label={`${label} closed`}
            />
          </div>
        );
      })}
      <p className="pt-1 text-xs text-muted-foreground">
        Times use your tenant timezone. Handoffs outside these windows fall
        through to your after-hours message.
      </p>
    </div>
  );
}
