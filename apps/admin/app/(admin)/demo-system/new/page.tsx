/**
 * New demo page — ADR-066. Server Component wraps the client form.
 */

import { CreateDemoForm } from "@/components/demos/CreateDemoForm";

export default function NewDemoPage() {
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">New demo</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Create a demo brand and its trigger code. Customers type the code on
          your live channels to start the roleplay.
        </p>
      </div>
      <CreateDemoForm />
    </div>
  );
}
