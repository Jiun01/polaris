"""Non-production E2E seed/teardown routes.

Extracted from ``routers/internal_e2e.py`` in Batch E (task E6; see PRP plan
audit-batch-e-structural-splits) into its own package, out of the prod router
tree (``routers/``). Conditionally mounted at ``/internal/e2e`` per
``main.py``'s environment gate — only when ``POLARIS_E2E_INTERNAL_SECRET`` is
set, and never in staging/prod (fail-closed ``RuntimeError`` at boot). No
re-exports — ``main.py`` imports ``polaris_api.e2e.router`` directly.
"""
