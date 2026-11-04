"""weekly_digest — asyncio.to_thread SMTP fix (silent-failure/resource-waste
audit A3, 2026-07-02). Confirms _send_digest is offloaded via to_thread (runs
off the event-loop thread) and the digest fires unconditionally.
"""
from __future__ import annotations

import threading
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")


class _FakeConn:
    pass


class _FakeAcquire:
    async def __aenter__(self) -> _FakeConn:
        return _FakeConn()

    async def __aexit__(self, *_a: object) -> None:
        return None


class _FakePool:
    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire()


async def test_digest_send_offloaded_from_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_send_digest must run off the event-loop thread (to_thread), not
    inline — a slow SMTP connection must not block the event loop."""
    from polaris_api.scheduled import weekly_digest
    from polaris_api.services import mrr_rollup

    thread_names: list[str] = []

    def fake_send_digest(mrr: float, settings: object) -> None:
        thread_names.append(threading.current_thread().name)

    monkeypatch.setattr(weekly_digest, "_send_digest", fake_send_digest)
    monkeypatch.setattr(
        mrr_rollup,
        "compute_mrr",
        AsyncMock(return_value={"mrr_myr": 12_000.0, "alert_band": "green"}),
    )

    await weekly_digest._run(_FakePool())

    assert thread_names, "_send_digest was never called"
    assert thread_names[0] != "MainThread"  # ran on a to_thread worker


async def test_digest_fires_regardless_of_mrr(monkeypatch: pytest.MonkeyPatch) -> None:
    """The digest is a plain weekly report — it sends at any MRR value."""
    from polaris_api.scheduled import weekly_digest
    from polaris_api.services import mrr_rollup

    send_calls: list[float] = []

    def fake_send_digest(mrr: float, settings: object) -> None:
        send_calls.append(mrr)

    monkeypatch.setattr(weekly_digest, "_send_digest", fake_send_digest)
    monkeypatch.setattr(
        mrr_rollup,
        "compute_mrr",
        AsyncMock(return_value={"mrr_myr": 10_000.0, "alert_band": "green"}),
    )

    await weekly_digest._run(_FakePool())

    assert send_calls == [10_000.0]


async def test_digest_job_failure_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """A compute_mrr failure must not raise out of _run (logged + swallowed)."""
    from polaris_api.scheduled import weekly_digest
    from polaris_api.services import mrr_rollup

    monkeypatch.setattr(
        mrr_rollup, "compute_mrr", AsyncMock(side_effect=RuntimeError("db down")),
    )

    await weekly_digest._run(_FakePool())  # must not raise
