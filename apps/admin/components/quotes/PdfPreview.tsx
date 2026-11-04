/**
 * PDF preview widget (Phase 9 PR-9).
 *
 * Shows quote_code, expires_at, and a download button.
 * Download href -> /api/admin/quotes/{id}/pdf (backend redirects to signed URL).
 */

import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { adminRoutes } from "@/lib/api/routes";
import { formatMytDate } from "@/lib/format";
import type { SalesQuote } from "@/lib/types/billing";

interface Props {
  quote: SalesQuote;
}

export function PdfPreview({ quote }: Props) {
  return (
    <Card className="space-y-3 p-4">
      <div className="flex items-center justify-between">
        <div className="space-y-0.5">
          <p className="text-sm font-medium">Quote {quote.quote_code}</p>
          <p className="text-xs text-muted-foreground">
            Expires {formatMytDate(quote.expires_at)}
            {quote.prospect_company && <> &mdash; {quote.prospect_company}</>}
          </p>
        </div>
        <a
          href={adminRoutes.quotes.pdf(quote.id)}
          target="_blank"
          rel="noreferrer"
        >
          <Button variant="outline" size="sm">
            Download PDF
          </Button>
        </a>
      </div>
    </Card>
  );
}
