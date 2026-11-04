/**
 * /settings/dsar index — redirect to the export flow.
 *
 * The DSAR surface only has two concrete actions: `/settings/dsar/export`
 * and `/settings/dsar/delete`. Visiting the bare `/settings/dsar` path
 * previously 404'd. Export is the safe, non-destructive default, so we
 * land the operator there; deletion remains an explicit, separate route.
 */

import { redirect } from "next/navigation";

export default function DsarIndexPage() {
  redirect("/settings/dsar/export");
}
