"""Broadcast drain perf benchmark — Phase 6 PR-9.

End-to-end: seed N audience members in real Postgres, schedule a
broadcast, ZADD into real Redis, run the worker tick loop with a
mocked meta_whatsapp client, time the wall-clock until all
recipients reach a terminal state.

Skipped locally unless ``POLARIS_TEST_DATABASE_URL`` AND
``REDIS_URL`` are set. Mirrors the Phase 5 ``PERF_GATE`` convention:

    PERF_GATE=skip    → skip the assertion (timing-only smoke)
    PERF_GATE=ci      → ≤ 300 s threshold (Phase 6 SSOT staging target)
    PERF_GATE=strict  → ≤ 90 s threshold (local mock target)
    unset             → strict by default

Per the Phase 6 deep-dive §8.4, the SSOT contract is "1000 recipients
drain in ≤5 min on staging". Local with mocked Meta latency we expect
under 90s — the Meta HTTP RTT dominates real-world timing and is
zero here.

The test mocks ``meta_whatsapp.MetaWhatsAppClient.send_template`` so
no real WABA + sandbox is needed (per user request — WABA isn't
linked to Chatwoot yet). All other components are real: Postgres
schema applied by migration-check CI, Redis instance via REDIS_URL,
worker.run_forever() driven by manually pumping _tick_once() to
keep the test bounded.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from collections.abc import AsyncIterator
from typing import Final
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from polaris_api.db.pools import close_pools, get_service_pool, init_pools
from polaris_api.redis_pool import close_redis, init_redis
from polaris_api.services import meta_whatsapp
from polaris_api.services.broadcast import service as broadcast_service
from polaris_api.services.broadcast import worker as broadcast_worker
from polaris_shared_types import CreateBroadcastRequest, FilterAllOptedIn

from ._seed import (
    cleanup_tenant,
    seed_approved_template,
    seed_audience,
    seed_channel,
    seed_tenant,
)

# -----------------------------------------------------------------------------
# Gating
# -----------------------------------------------------------------------------

_RECIPIENT_COUNT: Final[int] = int(os.getenv("PERF_BROADCAST_N", "1000"))

# Thresholds in seconds — see module docstring.
_THRESHOLDS_SEC: Final[dict[str, float]] = {
    "strict": 90.0,
    "ci": 300.0,
    "skip": float("inf"),
}


def _threshold_seconds() -> float:
    gate = os.getenv("PERF_GATE", "strict").lower()
    return _THRESHOLDS_SEC.get(gate, _THRESHOLDS_SEC["strict"])


pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("POLARIS_TEST_DATABASE_URL")
        and os.environ.get("REDIS_URL")
        and os.environ.get("POLARIS_DB_URL_ANON")
    ),
    reason=(
        "perf test requires POLARIS_TEST_DATABASE_URL + REDIS_URL + "
        "POLARIS_DB_URL_ANON — skipping (init_pools() needs the migration-010 "
        "polaris_anon role; CI does not bootstrap it, so this is a operator-"
        "driven local sweep like the other E2E tests)"
    ),
)


# -----------------------------------------------------------------------------
# Fixtures — real Postgres + real Redis + mocked Meta
# -----------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def _bootstrap_pools() -> AsyncIterator[None]:
    """Initialise the real pools once per module; close at teardown.

    The worker uses ``get_service_pool()`` + ``get_redis()`` — both
    must be live before _tick_once() runs.
    """
    # The pool helpers read POLARIS_DATABASE_URL; perf test uses
    # POLARIS_TEST_DATABASE_URL so we shim that into place. Save +
    # restore to be a good module-scoped citizen.
    saved = os.environ.get("POLARIS_DATABASE_URL")
    os.environ["POLARIS_DATABASE_URL"] = os.environ["POLARIS_TEST_DATABASE_URL"]
    try:
        await init_pools()
        await init_redis()
        yield
    finally:
        await close_redis()
        await close_pools()
        if saved is None:
            os.environ.pop("POLARIS_DATABASE_URL", None)
        else:
            os.environ["POLARIS_DATABASE_URL"] = saved


@pytest_asyncio.fixture
async def _scaffolding(_bootstrap_pools) -> AsyncIterator[dict]:  # noqa: ANN001
    """Seed one tenant + channel + template + N audience members.

    Yields a dict carrying every id the test needs; teardown cascades
    via DELETE FROM tenants.
    """
    pool = get_service_pool()
    async with pool.acquire() as conn:
        tenant_id = await seed_tenant(conn, slug=f"perf-{uuid4().hex[:8]}")
        # Synthetic chatwoot_inbox_id — UNIQUE-per-row but never reached
        # because we mock Meta.
        inbox_id = int(time.time() * 1000) % 2_000_000_000
        channel_id = await seed_channel(
            conn,
            tenant_id=tenant_id,
            chatwoot_inbox_id=inbox_id,
        )
        template_id = await seed_approved_template(
            conn,
            tenant_id=tenant_id,
            tenant_channel_id=channel_id,
        )
        await seed_audience(conn, tenant_id=tenant_id, count=_RECIPIENT_COUNT)

        # Stash a fake WABA token in tenant_secrets so the worker's
        # _get_token path returns something usable. The mocked
        # send_template ignores the token anyway, but the worker
        # would short-circuit a recipient with TOKEN_MISSING if
        # secrets.get returned None.
        from polaris_api.services import secrets

        await secrets.put(
            conn,
            tenant_id,
            "meta_waba_access_token",
            b"PERF_TEST_FAKE_TOKEN",
        )

    yield {
        "tenant_id": tenant_id,
        "channel_id": channel_id,
        "template_id": template_id,
    }

    async with pool.acquire() as conn:
        await cleanup_tenant(conn, tenant_id=tenant_id)


@pytest.fixture
def _mock_send_template(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace MetaWhatsAppClient.send_template with an AsyncMock
    that returns a fresh wamid per call.

    The wamid carries a monotonic counter so the
    broadcast_recipients_wamid_idx UNIQUE constraint stays happy
    even across replays.
    """
    counter = {"n": 0}

    async def _fake_send(**kwargs: object) -> str:  # noqa: ARG001
        counter["n"] += 1
        return f"wamid.perf.{counter['n']:08d}"

    mock = AsyncMock(side_effect=_fake_send)
    monkeypatch.setattr(
        meta_whatsapp.MetaWhatsAppClient,
        "send_template",
        mock,
    )
    return mock


# -----------------------------------------------------------------------------
# The benchmark
# -----------------------------------------------------------------------------


async def _create_and_schedule(scaffolding: dict) -> UUID:
    """Create + schedule a broadcast over the seeded audience.

    Runs the service layer directly (no FastAPI roundtrip) so we
    isolate the worker drain timing from HTTP overhead.
    """
    pool = get_service_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)",
            str(scaffolding["tenant_id"]),
        )
        req = CreateBroadcastRequest(
            name=f"perf-{uuid4().hex[:8]}",
            tenant_channel_id=scaffolding["channel_id"],
            template_id=scaffolding["template_id"],
            audience_filter=FilterAllOptedIn(filter_kind="all_opted_in"),
            variables_template={"1": "Friend"},
            scheduled_at=None,
        )
        broadcast_id = await broadcast_service.create_broadcast(
            conn,
            tenant_id=scaffolding["tenant_id"],
            actor_user_id=None,
            req=req,
        )
        await broadcast_service.schedule_broadcast(
            conn,
            tenant_id=scaffolding["tenant_id"],
            broadcast_id=broadcast_id,
            actor_user_id=None,
        )
    return broadcast_id


async def _wait_for_terminal(
    tenant_id: UUID, broadcast_id: UUID, *, deadline: float
) -> str:
    """Pump _tick_once() until the broadcast hits a terminal state
    or the deadline expires. Returns the final status."""
    pool = get_service_pool()
    while time.perf_counter() < deadline:
        await broadcast_worker._tick_once()  # noqa: SLF001 — perf entry point

        async with pool.acquire() as conn:
            status = await conn.fetchval(
                "SELECT status FROM broadcasts WHERE id = $1",
                broadcast_id,
            )
        if status in ("completed", "cancelled", "errored"):
            return str(status)
        # Yield to the event loop; tick interval is shorter than the
        # production worker's 5s because we want to drain fast.
        await asyncio.sleep(0.05)
    return "TIMEOUT"


@pytest.mark.asyncio
async def test_drain_1000_recipients_under_threshold(
    _scaffolding: dict, _mock_send_template: AsyncMock
) -> None:
    """End-to-end drain timing.

    Per Phase 6 SSOT exit criteria (04_BUILD_PHASES_AND_SKILLS.md
    Phase 6): "1000-recipient broadcast drains in ≤5 min on
    staging". Local with mocked Meta we expect under 90s.
    """
    threshold_sec = _threshold_seconds()

    broadcast_id = await _create_and_schedule(_scaffolding)

    t0 = time.perf_counter()
    # Add a defensive ceiling 4x the gate threshold so a regression
    # doesn't hang the test indefinitely.
    deadline = t0 + max(threshold_sec * 4, 300.0)
    final_status = await _wait_for_terminal(
        _scaffolding["tenant_id"], broadcast_id, deadline=deadline
    )
    elapsed_sec = time.perf_counter() - t0

    # Pull final counters for visibility.
    pool = get_service_pool()
    async with pool.acquire() as conn:
        counters = await conn.fetchrow(
            """
            SELECT total_recipients, sent_count, delivered_count,
                   read_count, failed_count, skipped_count
              FROM broadcasts
             WHERE id = $1
            """,
            broadcast_id,
        )
    pending_count = await _count_pending(broadcast_id)

    print(
        "broadcast drain perf: "
        f"final_status={final_status} "
        f"elapsed={elapsed_sec:.2f}s "
        f"threshold={threshold_sec:.0f}s "
        f"counters={dict(counters) if counters else None} "
        f"pending={pending_count}"
    )

    assert final_status == "completed", (
        f"broadcast did not complete cleanly: status={final_status!r}, "
        f"pending recipients={pending_count}, elapsed={elapsed_sec:.1f}s"
    )
    assert counters is not None  # noqa: S101
    assert counters["total_recipients"] == _RECIPIENT_COUNT
    # Mocked Meta returns success on every call, so sent_count should
    # equal total_recipients exactly.
    assert counters["sent_count"] == _RECIPIENT_COUNT, (
        f"sent_count={counters['sent_count']} != "
        f"total_recipients={_RECIPIENT_COUNT}"
    )

    if os.getenv("PERF_GATE", "strict").lower() != "skip":
        assert elapsed_sec <= threshold_sec, (
            f"drain took {elapsed_sec:.1f}s > threshold {threshold_sec:.0f}s "
            f"(PERF_GATE={os.getenv('PERF_GATE', 'strict')})"
        )


async def _count_pending(broadcast_id: UUID) -> int:
    pool = get_service_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM broadcast_recipients "
            "WHERE broadcast_id = $1 AND status = 'pending'",
            broadcast_id,
        )
    return int(n or 0)


# -----------------------------------------------------------------------------
# Per-tick latency stats — informational, never gates
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_latency_stats(
    _scaffolding: dict, _mock_send_template: AsyncMock
) -> None:
    """Run ~20 ticks against an idle queue + report p95 tick latency.

    Useful as an early signal if tick overhead regresses under
    Postgres index bloat — the SCAN + ZRANGEBYSCORE should run in
    sub-100ms even on a cold cache.
    """
    broadcast_id = await _create_and_schedule(_scaffolding)
    # First few ticks drain real recipients; collect timings AFTER
    # the broadcast completes so we measure pure overhead.
    deadline = time.perf_counter() + _threshold_seconds() * 4
    await _wait_for_terminal(
        _scaffolding["tenant_id"], broadcast_id, deadline=deadline
    )

    timings_ms: list[float] = []
    for _ in range(20):
        t0 = time.perf_counter()
        await broadcast_worker._tick_once()  # noqa: SLF001
        timings_ms.append((time.perf_counter() - t0) * 1000)

    median_ms = statistics.median(timings_ms)
    p95_ms = statistics.quantiles(timings_ms, n=20)[-1]
    print(
        "broadcast tick-latency (idle): "
        f"n=20 median={median_ms:.1f}ms p95={p95_ms:.1f}ms "
        f"max={max(timings_ms):.1f}ms"
    )
    # No assertion — this is observational only at GA. A future PR
    # may tighten this once we have staging baselines.
