"""Follow-up worker subprocess entrypoint — Phase 13 follow-ups PR-1.

Run with:

    uv run python apps/api/scripts/run_followup_worker.py

The follow-up tick also runs in-process inside the API lifespan (see
main.py), so this standalone entrypoint is for ops parity with the
reminder/broadcast workers — e.g. running the drain as its own process /
container, isolated from request serving. Bootstraps the same pools + KMS
sentinel, then enters ``followups.worker.run_forever``. SIGINT / SIGTERM
cancel the loop cleanly.

Env vars:
    FOLLOWUP_TICK_SECONDS   — poll interval (default 60)
    FOLLOWUP_MAX_ATTEMPTS   — max send attempts per job (default 3)
"""

from __future__ import annotations

import asyncio
import logging
import signal

from polaris_api.db.pools import close_pools, get_service_pool, init_pools
from polaris_api.redis_pool import close_redis, init_redis
from polaris_api.services.followups.worker import run_forever
from polaris_api.services.kms_sentinel import verify_sentinel
from polaris_api.utils.logging import configure_logging

logger = logging.getLogger(__name__)


async def _main() -> None:
    configure_logging()
    logger.info("run_followup_worker: bootstrapping pools")
    await init_pools()
    await init_redis()
    try:
        async with get_service_pool().acquire() as conn:
            await verify_sentinel(conn)
    except Exception:
        logger.exception("run_followup_worker: KMS sentinel verify failed; aborting")
        await close_redis()
        await close_pools()
        raise

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("run_followup_worker: received signal, draining")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    worker_task = asyncio.create_task(run_forever(), name="followup-worker")

    try:
        done, _pending = await asyncio.wait(
            {worker_task, asyncio.create_task(stop_event.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if worker_task in done:
            exc = worker_task.exception()
            if exc is not None:
                logger.exception(
                    "run_followup_worker: worker died with exception", exc_info=exc
                )
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        await close_redis()
        await close_pools()
        logger.info("run_followup_worker: shutdown clean")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("run_followup_worker: KeyboardInterrupt; exiting")
