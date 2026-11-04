/**
 * Same-origin invoice-PDF download for the admin app.
 *
 * The "Download PDF" link points here (same origin → carries the NextAuth
 * session cookie). The generic /api/proxy handler is JSON-only and can't carry
 * the PDF binary or pass through the FastAPI 307, so this dedicated handler:
 *
 *   1. mints a platform-admin JWT from the session (bearerForApi),
 *   2. calls FastAPI GET /api/admin/billing/invoices/{id}/pdf with that bearer —
 *      fetch FOLLOWS the 307 to the time-limited signed Supabase URL (the bearer
 *      is dropped on the cross-origin hop; the signed-URL token authorizes it),
 *   3. streams the resulting PDF bytes back to the browser.
 *
 * (We stream rather than redirect because undici's redirect:"manual" returns an
 * opaque response whose Location can't be read.)
 */

import { type NextRequest, NextResponse } from "next/server";

import { bearerForApi, SERVER_BASE_URL } from "@/lib/api/server";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function GET(
  _req: NextRequest,
  { params }: { params: { invoice_id: string } },
): Promise<NextResponse> {
  const bearer = await bearerForApi();
  if (!bearer) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  // Validate the id is a UUID before building the upstream URL. This route has
  // no path-prefix allowlist like /api/proxy, so a non-UUID segment (e.g. an
  // encoded "../") must not be allowed to shape the outbound FastAPI path.
  if (!UUID_RE.test(params.invoice_id)) {
    return NextResponse.json({ error: "invalid_invoice_id" }, { status: 400 });
  }

  const upstream = `${SERVER_BASE_URL.replace(/\/$/, "")}/api/admin/billing/invoices/${encodeURIComponent(
    params.invoice_id,
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
    // 404 (no PDF), 401/403 (auth), etc. — surface the upstream status.
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
      "content-disposition": `inline; filename="invoice-${params.invoice_id}.pdf"`,
      "cache-control": "no-store",
    },
  });
}
