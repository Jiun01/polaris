/**
 * New sales quote page (Phase 9 PR-9).
 * Client wrapper for QuoteGeneratorForm.
 */

import Link from "next/link";
import { QuoteGeneratorForm } from "@/components/quotes/QuoteGeneratorForm";
import { Card } from "@/components/ui/card";

export default function NewQuotePage() {
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Link
        href="/quotes"
        className="text-xs text-muted-foreground hover:text-foreground hover:underline"
      >
        Back to Sales Quotes
      </Link>
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Generate quote</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Build a proration-aware PDF quote for a prospect.
        </p>
      </div>
      <Card className="p-6">
        <QuoteGeneratorForm />
      </Card>
    </div>
  );
}
