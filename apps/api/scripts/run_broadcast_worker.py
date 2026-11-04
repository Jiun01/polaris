"""Broadcast worker subprocess entrypoint — Phase 6 PR-6.

Run with:

    uv run python apps/api/scripts/run_broadcast_worker.py

Initialises the pools + KMS sentinel just like the main API
process (same lifespan() shape minus the FastAPI app), then enters
``services/broadcast/worker.run_forever()``. SIGINT / SIGTERM
cancel the loop cleanly.

This subprocess is a separate process from the FastAPI server so
the broadcast worker scales independently + isolates failure
domains (a stuck Meta connection on a broadcast send doesn't pin
the request-path event loop).

In a docker-compose dev setup, run this as a second service that
shares the env. In production we'll wire it via a separate process
manager once deploy infra lands.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from polaris_api.db.pools import close_pools, get_service_pool, init_pools
from polaris_api.redis_pool import close_redis, init_redis
from polaris_api.services.broadcast.worker import run_forever
from polaris_api.services.kms_sentinel import verify_sentinel
from polaris_api.utils.logging import configure_logging

logger = logging.getLogger(__name__)


async def _main() -> None:
    configure_logging()
    logger.info("run_broadcast_worker: bootstrapping pools")
    await init_pools()
    await init_redis()
    try:
        async with get_service_pool().acquire() as conn:
            await verify_sentinel(conn)
    except Exception:
        logger.exception("run_broadcast_worker: KMS sentinel verify failed; aborting")
        await close_redis()
        await close_pools()
        raise

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("run_broadcast_worker: received signal, draining")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # add_signal_handler isn't supported on Windows; fall through
        # to the default KeyboardInterrupt handling there.
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    worker_task = asyncio.create_task(run_forever(), name="broadcast-worker")

    try:
        # Race the worker against the stop signal.
        done, _pending = await asyncio.wait(
            {worker_task, asyncio.create_task(stop_event.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if worker_task in done:
            # Worker exited unexpectedly (it's supposed to run forever).
            exc = worker_task.exception()
            if exc is not None:
                logger.exception(
                    "run_broadcast_worker: worker died with exception",
                    exc_info=exc,
                )
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        await close_redis()
        await close_pools()
        logger.info("run_broadcast_worker: shutdown clean")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("run_broadcast_worker: KeyboardInterrupt; exiting")
