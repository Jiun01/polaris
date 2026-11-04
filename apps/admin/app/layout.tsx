/**
 * Root layout (Phase 9 PR-3).
 *
 * Loads global Tailwind + shadcn CSS, registers the sonner toast layer
 * (top-level so toasts persist across route-group changes), sets the
 * Polaris Admin metadata, and renders the route-group children.
 */

import type { Metadata } from "next";
import { Toaster } from "sonner";

import "./globals.css";

export const metadata: Metadata = {
  title: "Polaris Admin",
  description: "Polaris v2 internal operations console",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="bg-background text-foreground antialiased">
        {children}
        <Toaster richColors closeButton position="top-right" />
      </body>
    </html>
  );
}
