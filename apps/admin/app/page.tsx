/**
 * Root index (Phase 9 PR-3).
 *
 * Authenticated users land here briefly while middleware decides where
 * they should go. We pre-empt with a server-side redirect to /tenants
 * so signed-in admins get straight to work without a flash of an empty
 * page. Unauthenticated users are bounced to /login by the middleware
 * before they reach this component.
 */

import { redirect } from "next/navigation";

export default function HomePage(): never {
  redirect("/tenants");
}
