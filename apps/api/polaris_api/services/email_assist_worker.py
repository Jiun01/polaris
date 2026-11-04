"""Email-assist worker — email channel M2.

Drains the email-assist queue and posts one draft private note per inbound
email. Cross-tenant like the follow-up worker: uses the service pool + Redis +
Chatwoot client singletons (NOT ``PipelineDeps``), because the assist runs for
every tenant and writes the service-scoped ``email_drafts`` table.

Per-job errors are caught + logged so one bad email can't kill the loop;
cancellation propagates so lifespan shutdown is clean (mirrors
``inbound_worker``).
"""

from __future__ import annotations

import asyncio
import logging

from polaris_api.services import email_assist
from polaris_api.services.email_assist_queue import get_queue

logger = logging.getLogger(__name__)


async def run_email_assist_worker() -> None:
    """Drain the email-assist queue forever, posting a draft note per job.

    Caller (main.py lifespan) spawns this as
    ``asyncio.create_task(run_email_assist_worker())`` once pools + Redis are up.
    The task lives until cancellation."""
    # Imported lazily so importing this module doesn't require an initialised
    # pool/redis (matches the followup worker's deferred singleton access).
    from polaris_api.db.pools import get_service_pool
    from polaris_api.redis_pool import get_redis
    from polaris_api.services.chatwoot.client import get_chatwoot_client

    queue = get_queue()
    pool = get_service_pool()
    redis = get_redis()
    chatwoot = get_chatwoot_client()
    logger.info("email_assist_worker: started")
    try:
        while True:
            job = await queue.get()
            try:
                await email_assist.generate_and_post_draft(
                    job, pool=pool, redis=redis, chatwoot=chatwoot
                )
            except Exception as exc:  # noqa: BLE001
                # Never let a single bad email kill the worker. The note is the
                # only side effect; a failure just means no draft this round.
                logger.error(
                    "email_assist_worker: dropped tenant=%s conv=%s reason=%s detail=%s",
                    job.tenant_id,
                    job.chatwoot_conversation_id,
                    type(exc).__name__,
                    exc,
                )
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        logger.info("email_assist_worker: stopping")
        raise
