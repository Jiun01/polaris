"""Booking reminder worker subprocess entrypoint — Phase 13 PR-1.

Run with:

    uv run python apps/api/scripts/run_reminder_worker.py

Bootstraps the same pools + KMS sentinel as run_broadcast_worker.py,
then enters a tick loop that calls
``services.booking_reminders.tick_due()`` every REMINDER_TICK_SECONDS
seconds (default 60). SIGINT / SIGTERM cancel the loop cleanly.

In a docker-compose dev setup, add this as a second service alongside
the broadcast worker. The two workers are independent processes — a
stuck Meta call on a reminder never blocks a broadcast and vice versa.

Env vars:
    REMINDER_TICK_SECONDS      — poll interval (default 60)
    REMINDER_MAX_ATTEMPTS      — max send attempts per reminder (default 3)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from polaris_api.db.pools import close_pools, get_service_pool, init_pools
from polaris_api.redis_pool import close_redis, init_redis
from polaris_api.services.booking_reminders import tick_due
from polaris_api.services.kms_sentinel import verify_sentinel
from polaris_api.services.meta_whatsapp import get_meta_whatsapp_client
from polaris_api.utils.logging import configure_logging

logger = logging.getLogger(__name__)

_DEFAULT_TICK_SECONDS = 60


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


async def _run_forever() -> None:
    tick = _env_int("REMINDER_TICK_SECONDS", _DEFAULT_TICK_SECONDS)
    meta_client = get_meta_whatsapp_client()
    logger.info("reminder worker: starting (tick=%ds)", tick)

    while True:
        try:
            async with get_service_pool().acquire() as conn:
                sent = await tick_due(conn, meta_client=meta_client)
            if sent:
                logger.info("reminder worker: sent %d reminder(s)", sent)
        except asyncio.CancelledError:
            logger.info("reminder worker: cancelled, stopping")
            raise
        except Exception:  # noqa: BLE001
            logger.exception("reminder worker: tick failed; sleeping + retrying")

        await asyncio.sleep(tick)


async def _main() -> None:
    configure_logging()
    logger.info("run_reminder_worker: bootstrapping pools")
    await init_pools()
    await init_redis()
    try:
        async with get_service_pool().acquire() as conn:
            await verify_sentinel(conn)
    except Exception:
        logger.exception("run_reminder_worker: KMS sentinel verify failed; aborting")
        await close_redis()
        await close_pools()
        raise

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("run_reminder_worker: received signal, draining")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    worker_task = asyncio.create_task(_run_forever(), name="reminder-worker")

    try:
        done, _pending = await asyncio.wait(
            {worker_task, asyncio.create_task(stop_event.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if worker_task in done:
            exc = worker_task.exception()
            if exc is not None:
                logger.exception(
                    "run_reminder_worker: worker died with exception",
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
        logger.info("run_reminder_worker: shutdown clean")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("run_reminder_worker: KeyboardInterrupt; exiting")
