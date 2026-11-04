"""GET /health — Phase 0 contract per docs/Polaris V2/04a_PHASE_0_DEEP.md §6.

Returns `HealthStatus`:
  status:    "ok" | "degraded" | "down"
  supabase:  "reachable" | "unreachable"
  redis:     "reachable" | "unreachable"
  version:   commit SHA short (or "dev" locally)

Never 5xx — health endpoint's purpose is liveness; degradation is reported as 200.

Architecture:
- HealthStatus is imported from `polaris_shared_types` (the SSOT), not redefined
  locally. Previously this module shadowed the shared model; both could drift.
- Probes run in parallel via `asyncio.gather` (worst-case latency = max, not sum).
- The `gather_probes` dependency lets tests override the whole probe surface via
  `app.dependency_overrides[gather_probes]` instead of monkeypatching module-level
  symbols, which is brittle once probes move into shared dependency graphs.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends
from polaris_shared_types import HealthStatus

from polaris_api.config import get_settings
from polaris_api.deps.health import ProbeResult, probe_redis, probe_supabase

router = APIRouter(tags=["health"])

StatusLiteral = Literal["ok", "degraded", "down"]
ReachLiteral = Literal["reachable", "unreachable"]


async def gather_probes() -> tuple[ProbeResult, ProbeResult]:
    """Run both probes concurrently. Returns (supabase, redis) tuple."""
    supabase, redis = await asyncio.gather(probe_supabase(), probe_redis())
    return supabase, redis


def _aggregate_status(supabase_ok: bool, redis_ok: bool) -> StatusLiteral:
    if supabase_ok and redis_ok:
        return "ok"
    if not supabase_ok and not redis_ok:
        return "down"
    return "degraded"


def _reach(ok: bool) -> ReachLiteral:
    return "reachable" if ok else "unreachable"


@router.get(
    "/health",
    response_model=HealthStatus,
    summary="Service health",
    description=(
        "Liveness + dependency-reachability probe. Always returns HTTP 200; "
        "the `status` field reports `ok` / `degraded` / `down`. Load balancers "
        "and Kubernetes-style orchestrators should poll this, not `/`."
    ),
)
async def get_health(
    probes: tuple[ProbeResult, ProbeResult] = Depends(gather_probes),
) -> HealthStatus:
    """Probe Supabase + Redis concurrently; report aggregate status."""
    supabase_probe, redis_probe = probes

    return HealthStatus(
        status=_aggregate_status(supabase_probe.reachable, redis_probe.reachable),
        supabase=_reach(supabase_probe.reachable),
        redis=_reach(redis_probe.reachable),
        version=get_settings().version,
    )
