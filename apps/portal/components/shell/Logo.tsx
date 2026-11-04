import { cn } from "@/lib/utils";

/**
 * Polaris wordmark logo.
 *
 * The SVG is TWO-color (dark/cream wordmark + terracotta `#c85a32` star),
 * so it cannot be tinted via `currentColor` — we swap the light/dark file
 * by theme. `LightLogo` (dark wordmark) shows on light backgrounds;
 * `DarkLogo` (cream wordmark) shows under `.dark`. Both share the star.
 *
 * Plain <img> (not next/image) so the static SVG is served verbatim from
 * /public without the image optimizer's SVG guard. viewBox is
 * 573.06 × 162.94 (aspect ≈ 3.52:1); height drives size, width is auto.
 *
 * Assets live under /public/brand/ — the auth middleware matcher excludes
 * `/brand` (see middleware.ts), so these load on public (unauthenticated)
 * pages like /login. A root-level /public path would be redirected to /login.
 */
export function Logo({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex items-center", className)}>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src="/brand/polaris-logo.svg"
        alt="Polaris"
        className="h-[26px] w-auto dark:hidden"
      />
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src="/brand/polaris-logo-dark.svg"
        alt="Polaris"
        className="hidden h-[26px] w-auto dark:block"
      />
    </span>
  );
}
