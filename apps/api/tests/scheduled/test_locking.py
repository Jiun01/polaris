"""Advisory-lock helper tests — phase-9 scheduler double-fire guard.

``run_with_advisory_lock`` must:
  * run the job exactly once when it wins the lock, then release it;
  * NOT run the job when another instance holds the lock;
  * always release the lock even when the job raises.
"""

from __future__ import annotations

from typing import Any

import pytest
from polaris_api.scheduled._locking import (
    JOB_LOCK_KEYS,
    run_with_advisory_lock,
)

pytestmark = pytest.mark.asyncio(loop_scope="function")


class _FakeConn:
    def __init__(self, *, lock_granted: bool) -> None:
        self._lock_granted = lock_granted
        self.unlocked = False

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "pg_try_advisory_lock" in sql:
            return self._lock_granted
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        if "pg_advisory_unlock" in sql:
            self.unlocked = True
        return "OK"


class _Acquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


async def test_runs_job_and_unlocks_when_lock_acquired() -> None:
    conn = _FakeConn(lock_granted=True)
    pool = _FakePool(conn)
    ran = []

    async def _job() -> None:
        ran.append(True)

    await run_with_advisory_lock(
        pool, job_name="mrr_rollup", lock_key=JOB_LOCK_KEYS["mrr_rollup"], job=_job
    )

    assert ran == [True]
    assert conn.unlocked is True


async def test_skips_job_when_lock_held_elsewhere() -> None:
    conn = _FakeConn(lock_granted=False)
    pool = _FakePool(conn)
    ran = []

    async def _job() -> None:
        ran.append(True)

    await run_with_advisory_lock(
        pool, job_name="mrr_rollup", lock_key=JOB_LOCK_KEYS["mrr_rollup"], job=_job
    )

    assert ran == []  # job never ran
    assert conn.unlocked is False  # nothing to release


async def test_unlocks_even_when_job_raises() -> None:
    conn = _FakeConn(lock_granted=True)
    pool = _FakePool(conn)

    async def _job() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await run_with_advisory_lock(
            pool,
            job_name="mrr_rollup",
            lock_key=JOB_LOCK_KEYS["mrr_rollup"],
            job=_job,
        )

    assert conn.unlocked is True


async def test_job_lock_keys_are_unique() -> None:
    keys = list(JOB_LOCK_KEYS.values())
    assert len(keys) == len(set(keys))
