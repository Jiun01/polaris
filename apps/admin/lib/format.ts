/**
 * Admin-side formatters (Phase 9 PR-3).
 *
 * MYR currency, RFC3339 → MYT (Asia/Kuala_Lumpur) display, and
 * cost-band coloring helpers. Mirrors apps/portal/lib/format.ts in
 * spirit but the admin uses a cost-band scale (amber@50%, red@100%)
 * — see 04j §9 — whereas the portal uses a quota-band scale.
 */

const MYT_TZ = "Asia/Kuala_Lumpur";

const myrFormatter = new Intl.NumberFormat("en-MY", {
  style: "currency",
  currency: "MYR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const intFormatter = new Intl.NumberFormat("en-MY");

/** Format a MYR amount with the `RM 1,234.56` convention. */
export function formatMyr(amount: number | string): string {
  const n = typeof amount === "string" ? Number(amount) : amount;
  if (!Number.isFinite(n)) return "—";
  return myrFormatter.format(n);
}

/** Thousands-separated integer; em-dash on non-finite. */
export function formatInt(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  return intFormatter.format(n);
}

/**
 * Render an RFC3339 UTC timestamp in MYT (Asia/Kuala_Lumpur). Returns
 * em-dash for null / undefined / unparseable input.
 */
export function formatMyt(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-MY", {
    timeZone: MYT_TZ,
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
}

/** Date-only version of formatMyt. */
export function formatMytDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-MY", {
    timeZone: MYT_TZ,
    year: "numeric",
    month: "short",
    day: "2-digit",
  }).format(d);
}
