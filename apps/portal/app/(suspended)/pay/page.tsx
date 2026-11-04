/**
 * Payment-overdue lockdown page (Phase 8 PR-2).
 *
 * Per ADR-049: when `tenants.status='suspended'` AND
 * `tenants.suspended_reason='payment_overdue'`, every portal route
 * redirects to `/pay`. The tenant sees outstanding invoices, bank
 * transfer details, and an "I've paid" support CTA.
 *
 * At GA the invoice fetch is stubbed: PR-10 will land the live
 * `/api/tenant/billing/invoices` consumer + a polished invoices list.
 * For PR-2 we render a static "contact billing" card so the lockdown
 * works end-to-end (tenant is locked out; tenant has a clear next
 * action) without depending on PR-10's billing surface.
 *
 * The bank-transfer details are placeholders — operations will edit
 * this file with the real account info before tenants live. Recording
 * them here vs an env var is intentional: bank account info is not a
 * secret, and committing the values to source means they're visible
 * in PR review when changed.
 */

export default function PaymentOverduePage() {
  return (
    <div className="rounded-xl bg-card p-8 outline outline-1 -outline-offset-1 outline-destructive/30">
      <div className="mb-6 flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-full bg-destructive/10">
          <svg
            className="h-5 w-5 text-destructive"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden
          >
            <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
            <line x1="12" y1="9" x2="12" y2="13" />
            <line x1="12" y1="17" x2="12.01" y2="17" />
          </svg>
        </div>
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-foreground">
            Your account is suspended for an unpaid invoice
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Sign-in still works, but the portal stays locked until payment
            clears.
          </p>
        </div>
      </div>

      <section className="mb-6">
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          Outstanding invoices
        </h2>
        <div className="rounded-lg bg-muted px-4 py-3 text-sm text-muted-foreground outline outline-1 -outline-offset-1 outline-border">
          A detailed invoice breakdown lands with the billing dashboard in a
          follow-up release. For now, contact{" "}
          <a
            href="mailto:billing@getpolaris.my"
            className="font-medium text-primary underline-offset-4 hover:underline"
          >
            billing@getpolaris.my
          </a>{" "}
          for your outstanding balance and reference number.
        </div>
      </section>

      <section className="mb-6">
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          Pay by bank transfer
        </h2>
        <dl className="grid grid-cols-1 gap-2 rounded-lg bg-card px-4 py-3 text-sm outline outline-1 -outline-offset-1 outline-border sm:grid-cols-3">
          <div>
            <dt className="text-xs text-muted-foreground">Account name</dt>
            <dd className="text-foreground">Polaris SDN BHD</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Bank</dt>
            <dd className="text-foreground">Maybank</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Account number</dt>
            <dd className="text-foreground">
              <span className="italic text-muted-foreground">
                Provided by billing on request
              </span>
            </dd>
          </div>
        </dl>
        <p className="mt-2 text-xs text-muted-foreground">
          Use your tenant slug as the payment reference so the billing team can
          match it to your account.
        </p>
      </section>

      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <a
          href="mailto:billing@getpolaris.my?subject=I%27ve%20paid%20%E2%80%94%20notify%20support"
          className="inline-flex items-center justify-center rounded-lg bg-primary px-4 py-2 text-sm font-[460] text-primary-foreground transition-all duration-100 ease-out hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          I&apos;ve paid &mdash; notify support
        </a>
        <p className="text-xs text-muted-foreground">
          Once payment clears, support will reinstate your account. Bot replies
          and agent inbox access resumes automatically.
        </p>
      </div>
    </div>
  );
}
