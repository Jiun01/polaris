/**
 * Cross-component formatting helpers (Phase 8 PR-3).
 *
 * MYR currency, RFC3339→local time, and percent-dial color helpers
 * used by Dashboard / Billing / quota surfaces. Centralised so a
 * future ringgit display rule (e.g. round to nearest 50 sen) lands
 * once.
 */

// Module-level cached formatter — constructing an Intl.NumberFormat is
// relatively expensive, so build the MYR formatter once instead of per call.
const MYR_FORMAT = new Intl.NumberFormat("en-MY", {
  style: "currency",
  currency: "MYR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** Format a number of MYR cents-or-decimal as "RM 1,234.56". */
export function formatMyr(amount: number): string {
  return MYR_FORMAT.format(amount);
}

/** Format an integer count with thousands separators. */
export function formatCount(n: number): string {
  return new Intl.NumberFormat("en-MY").format(n);
}

/** RFC3339 / ISO string → short relative ("2h ago" or "in 2h"). */
export function formatRelative(rfc3339: string, nowMs = Date.now()): string {
  const then = Date.parse(rfc3339);
  if (Number.isNaN(then)) return "—";
  const deltaMs = nowMs - then;
  const future = deltaMs < 0;
  const sec = Math.round(Math.abs(deltaMs) / 1000);
  const fmt = (n: number, unit: string) =>
    future ? `in ${n}${unit}` : `${n}${unit} ago`;
  if (sec < 60) return fmt(sec, "s");
  const min = Math.round(sec / 60);
  if (min < 60) return fmt(min, "m");
  const hr = Math.round(min / 60);
  if (hr < 24) return fmt(hr, "h");
  const day = Math.round(hr / 24);
  if (day < 30) return fmt(day, "d");
  const mo = Math.round(day / 30);
  if (mo < 12) return fmt(mo, "mo");
  return fmt(Math.round(mo / 12), "y");
}

/** RFC3339 → "20 May 2026, 14:32" (en-MY local). */
export function formatAbsolute(rfc3339: string): string {
  const d = new Date(rfc3339);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-MY", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(d);
}

/** % used → "32%". */
export function formatPercent(used: number, cap: number): string {
  if (cap <= 0) return "—";
  return `${Math.min(100, Math.round((used / cap) * 100))}%`;
}

/**
 * Color band for a quota dial based on % used. Returns a Tailwind
 * utility set; consumers spread it into className.
 */
export function quotaDialColor(
  used: number,
  cap: number,
): {
  ring: string;
  text: string;
  band: "ok" | "amber" | "red";
} {
  if (cap <= 0)
    return { ring: "ring-border", text: "text-muted-foreground", band: "ok" };
  const pct = (used / cap) * 100;
  if (pct >= 100) {
    return {
      ring: "ring-destructive",
      text: "text-destructive",
      band: "red",
    };
  }
  if (pct >= 80) {
    return {
      ring: "ring-warning",
      text: "text-warning",
      band: "amber",
    };
  }
  return {
    ring: "ring-border",
    text: "text-foreground",
    band: "ok",
  };
}
