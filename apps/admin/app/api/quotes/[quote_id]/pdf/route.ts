/**
 * Same-origin sales-quote-PDF download for the admin app.
 *
 * Mirrors app/api/billing/invoices/[invoice_id]/pdf — the generic /api/proxy
 * handler is JSON-only and can't carry binary or pass through FastAPI's 307, and
 * a direct /api/admin/... <a href> would 404 on the admin origin (that path lives
 * on the FastAPI service). This handler mints a platform-admin JWT, calls FastAPI
 * GET /api/admin/quotes/{id}/pdf (fetch follows the 307 to the signed Supabase
 * URL), and streams the PDF bytes back.
 */

import { type NextRequest, NextResponse } from "next/server";

import { bearerForApi, SERVER_BASE_URL } from "@/lib/api/server";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function GET(
  _req: NextRequest,
  { params }: { params: { quote_id: string } },
): Promise<NextResponse> {
  const bearer = await bearerForApi();
  if (!bearer) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  // Validate the id is a UUID before building the upstream URL — this route has
  // no path-prefix allowlist, so a non-UUID segment must not shape the path.
  if (!UUID_RE.test(params.quote_id)) {
    return NextResponse.json({ error: "invalid_quote_id" }, { status: 400 });
  }

  const upstream = `${SERVER_BASE_URL.replace(/\/$/, "")}/api/admin/quotes/${encodeURIComponent(
    params.quote_id,
  )}/pdf`;

  let res: Response;
  try {
    res = await fetch(upstream, {
      headers: { Authorization: `Bearer ${bearer}`, Accept: "application/pdf" },
      cache: "no-store",
    });
  } catch {
    return NextResponse.json(
      { error: "pdf_upstream_unreachable" },
      { status: 502 },
    );
  }

  if (!res.ok) {
    return NextResponse.json(
      { error: "pdf_unavailable", upstream_status: res.status },
      { status: res.status >= 400 ? res.status : 502 },
    );
  }

  const bytes = await res.arrayBuffer();
  return new NextResponse(bytes, {
    status: 200,
    headers: {
      "content-type": res.headers.get("content-type") ?? "application/pdf",
      "content-disposition": `inline; filename="quote-${params.quote_id}.pdf"`,
      "cache-control": "no-store",
    },
  });
}
