"""Admin-scope service helpers — Phase 3 PR-4.

This package exists to satisfy the ``services/admin/`` allowlist entry in
``apps/api/scripts/lint_service_pool.py``. Any code that legitimately
needs the BYPASSRLS service pool for ``/api/admin/*`` routes lives under
this path so the lint gate keeps tenant-scope code honest.

PR-4 lands a single helper: ``get_admin_service_conn``, the async
FastAPI dependency that admin routes use to hold a service-pool
connection for the duration of a request. See ``pool.py`` for the
contract and rationale.
"""

from polaris_api.services.admin.pool import get_admin_service_conn

__all__ = ["get_admin_service_conn"]
