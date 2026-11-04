"""Audience services — Phase 6 PR-4.

Three modules:

  * ``importer``  — CSV upload pipeline; line-error-accumulating
    parser + E.164 phone normalisation via libphonenumber + bulk
    INSERT under RLS. Backs ``POST /api/tenant/audience/import``.

  * ``opt_out``   — PDPA-blocking opt-out viewer + ``mark_opted_out``.
    The viewer surfaces under both ``/api/tenant/audience/opted-out``
    (portal JWT) AND the lone GA ``/api/v1/audience/opted-out``
    exception (also portal JWT — tenant API keys are v3.0 per
    ADR-035 Amendment evening).

  * ``filter``    — (PR-5) audience_filter discriminated-union → SQL
    WHERE compiler. Lives here because broadcast materialisation is
    audience-shaped, not broadcast-shaped (filter resolves against
    audience_members).
"""
