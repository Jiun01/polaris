/**
 * Same-origin CSV export for website leads.
 *
 * The "Export CSV" link points here (same origin → carries the NextAuth session
 * cookie). The generic /api/proxy handler is JSON-only and can't carry the CSV
 * body, so this dedicated handler — like the invoice-PDF route — mints a
 * platform-admin JWT, calls FastAPI GET /api/admin/leads/export with the active
 * filter, and streams the CSV back to the browser as a download.
 *
 * Only the `status` / `form_type` query params are forwarded (allowlisted) so a
 * crafted query string can't reshape the upstream request.
 */

import { type NextRequest, NextResponse } from "next/server";

import { bearerForApi, SERVER_BASE_URL } from "@/lib/api/server";

const ALLOWED_STATUS = new Set(["new", "contacted"]);
const ALLOWED_FORM_TYPE = new Set(["demo", "pricing", "contact", "website"]);

export async function GET(req: NextRequest): Promise<NextResponse> {
  const bearer = await bearerForApi();
  if (!bearer) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const status = req.nextUrl.searchParams.get("status") ?? "new";
  const formType = req.nextUrl.searchParams.get("form_type");
  if (!ALLOWED_STATUS.has(status)) {
    return NextResponse.json({ error: "invalid_status" }, { status: 400 });
  }
  if (formType !== null && !ALLOWED_FORM_TYPE.has(formType)) {
    return NextResponse.json({ error: "invalid_form_type" }, { status: 400 });
  }

  const params = new URLSearchParams({ status });
  if (formType) params.set("form_type", formType);
  const upstream = `${SERVER_BASE_URL.replace(/\/$/, "")}/api/admin/leads/export?${params.toString()}`;

  let res: Response;
  try {
    res = await fetch(upstream, {
      headers: { Authorization: `Bearer ${bearer}`, Accept: "text/csv" },
      cache: "no-store",
    });
  } catch {
    return NextResponse.json(
      { error: "export_upstream_unreachable" },
      { status: 502 },
    );
  }

  if (!res.ok) {
    return NextResponse.json(
      { error: "export_unavailable", upstream_status: res.status },
      { status: res.status >= 400 ? res.status : 502 },
    );
  }

  const body = await res.text();
  const suffix = formType ? `-${formType}` : "";
  return new NextResponse(body, {
    status: 200,
    headers: {
      "content-type": res.headers.get("content-type") ?? "text/csv",
      "content-disposition": `attachment; filename="leads-${status}${suffix}.csv"`,
      "cache-control": "no-store",
    },
  });
}
