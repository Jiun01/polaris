"use client";

/**
 * Mobile navigation drawer (Phase 8 PR-3).
 *
 * Sidebar is `hidden md:flex`, so on tablet/phone the user has no way
 * to reach Conversations / Knowledge / etc. without this. Opens from
 * a hamburger button in the TopBar. Uses Radix Dialog (the underlying
 * primitive of shadcn's Sheet) directly so we don't need to ship the
 * full Sheet abstraction this PR.
 *
 * Closes automatically when the user follows a link — `onOpenChange`
 * + `usePathname()` watching for changes would be cleaner, but a
 * Link with onClick is sufficient and obvious.
 */

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Lock, Menu, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Logo } from "@/components/shell/Logo";
import { NAV_ITEMS, isNavItemActive } from "@/components/shell/nav-items";
import { useTenantContext } from "@/components/providers/tenant-provider";
import { meetsTier } from "@/lib/tier-gate";
import { cn } from "@/lib/utils";

export function MobileNav() {
  const pathname = usePathname() ?? "/";
  const { plan_tier } = useTenantContext();
  const [open, setOpen] = React.useState(false);

  // Close the drawer when the route changes (link followed). We watch
  // pathname rather than wiring onClick onto every Link so future-added
  // links don't have to remember.
  const lastPathRef = React.useRef(pathname);
  React.useEffect(() => {
    if (open && lastPathRef.current !== pathname) {
      setOpen(false);
    }
    lastPathRef.current = pathname;
  }, [pathname, open]);

  return (
    <DialogPrimitive.Root open={open} onOpenChange={setOpen}>
      <DialogPrimitive.Trigger asChild>
        <Button
          variant="ghost"
          size="icon"
          aria-label="Open navigation"
          className="md:hidden"
        >
          <Menu className="h-4 w-4" aria-hidden />
        </Button>
      </DialogPrimitive.Trigger>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0" />
        <DialogPrimitive.Content
          className={cn(
            "fixed inset-y-0 left-0 z-50 flex w-72 max-w-[85vw] flex-col border-r border-border bg-card shadow-lg",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=closed]:slide-out-to-left data-[state=open]:slide-in-from-left",
            "duration-200",
          )}
        >
          <div className="flex h-14 items-center justify-between border-b border-border px-4">
            <DialogPrimitive.Title asChild>
              <Link href="/dashboard" className="flex items-center">
                <Logo />
                <span className="sr-only">Polaris</span>
              </Link>
            </DialogPrimitive.Title>
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" size="icon" aria-label="Close navigation">
                <X className="h-4 w-4" aria-hidden />
              </Button>
            </DialogPrimitive.Close>
          </div>
          <nav
            className="flex-1 overflow-y-auto py-3"
            aria-label="Primary navigation"
          >
            <ul className="space-y-0.5 px-2">
              {NAV_ITEMS.map((item) => {
                const active = isNavItemActive(pathname, item.href);
                const locked =
                  item.minTier !== undefined &&
                  !meetsTier(plan_tier, item.minTier);
                const Icon = item.icon;
                return (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        "group flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
                        active
                          ? "bg-accent text-accent-foreground"
                          : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                        locked && "opacity-70",
                      )}
                    >
                      <Icon className="h-4 w-4 shrink-0" aria-hidden />
                      <span className="flex-1 truncate">{item.label}</span>
                      {locked ? (
                        <Lock
                          className="h-3.5 w-3.5 text-muted-foreground"
                          role="img"
                          aria-label="Upgrade required"
                        />
                      ) : null}
                      {item.hint ? (
                        <Badge
                          variant="outline"
                          className="text-[10px] font-normal"
                        >
                          {item.hint}
                        </Badge>
                      ) : null}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </nav>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
