/**
 * Tailwind class composition helper (Phase 8 PR-3).
 *
 * Canonical shadcn/ui `cn()` — combines `clsx` (conditional class
 * assembly) with `tailwind-merge` (last-wins conflict resolution for
 * Tailwind utilities so component variants override parent classes
 * predictably).
 */

import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
