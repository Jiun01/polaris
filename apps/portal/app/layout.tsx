import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { ImpersonationBanner } from "@/components/shell/ImpersonationBanner";

// Inter is a variable font → exposes any weight (420/440/460/520/620 used by
// the Chatwoot-parity typography utilities). Wired to `fontFamily.sans` via
// the `--font-inter` CSS variable.
const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Polaris",
  description: "Polaris v2 portal (Phase 0 skeleton)",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={inter.variable}>
      <body className="min-h-screen bg-background text-foreground antialiased">
        <ImpersonationBanner />
        {children}
      </body>
    </html>
  );
}
