"""Tenant-audience router module-level tests — Phase 6 PR-4.

Route registration + main-app mount. Full DB integration runs in CI
against Postgres.
"""

from __future__ import annotations

from polaris_api.routers import (
    public_audience_optout as v1_module,
)
from polaris_api.routers import (
    tenant_audience as tenant_module,
)


def test_tenant_audience_router_exposes_all_endpoints() -> None:
    """Phase 6 PR-4 shipped import / import-from-conversations /
    opted-out / opted-out.csv; Phase 8 PR-8 added GET / for the
    Members tab. Pin the full set so a future refactor that drops
    the Members listing surfaces here."""
    paths = {r.path for r in tenant_module.router.routes}
    assert paths == {
        "/",  # Phase 8 PR-8 — Members tab listing
        "/import",
        "/import-from-conversations",
        "/opted-out",
        "/opted-out.csv",
    }


def test_public_audience_optout_router_exposes_one_endpoint() -> None:
    paths = {r.path for r in v1_module.router.routes}
    assert paths == {"/opted-out"}


def test_tenant_audience_routes_mounted_under_prefix() -> None:
    """Pin the absolute paths so future refactors don't silently
    relocate the Phase 8 portal contract."""
    from polaris_api.main import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/api/tenant/audience/import" in paths
    assert "/api/tenant/audience/import-from-conversations" in paths
    assert "/api/tenant/audience/opted-out" in paths
    assert "/api/tenant/audience/opted-out.csv" in paths


def test_v1_opted_out_route_mounted_as_ga_exception() -> None:
    """The /api/v1/audience/opted-out endpoint is the sole GA
    `/api/v1/*` route. Pin its existence so a refactor that drops
    `/api/v1/*` registration entirely doesn't slip through review."""
    from polaris_api.main import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/api/v1/audience/opted-out" in paths


def test_no_other_v1_routes_at_ga() -> None:
    """ADR-035 Am evening: the public `/api/v1/*` surface is deliberately
    minimal. Originally only /api/v1/audience/opted-out; off-Meta channels M3
    adds the public website-widget endpoints (config + logged redirect), which
    MUST be unauthenticated to be embeddable on tenant sites. Any OTHER
    `/api/v1/*` registration would be a regression on the v3.0-defer plan."""
    from polaris_api.main import app

    v1_paths = {
        route.path
        for route in app.routes
        if hasattr(route, "path") and route.path.startswith("/api/v1/")
    }
    assert v1_paths == {
        "/api/v1/audience/opted-out",
        "/api/v1/widget/{widget_key}/config",
    }
