"""Inbound-queue → pipeline worker — Phase 4 PR-4.

Replaces the Phase 2 placeholder logging consumer. Drains the in-
process ``inbound_queue`` and calls ``pipeline.run`` on each
``IncomingMessage``. Errors at the per-message boundary are caught
+ logged so a single malformed inbound can't kill the worker.

Per-message error policy:

  * ``LLMConfigError`` (placeholder Anthropic key in the env) →
    log warning, ACK the message, move on. This lets local dev boot
    without crashing the worker; the operator sees the log and
    pastes a real key.

  * ``LLMHardFailure`` (both providers down) → log error, ACK,
    move on. Customer doesn't get a reply this round; the message
    is dropped. Per ADR-031 the pipeline orchestrator's plan was to
    write an apology reply on this path — that lands in PR-6 along
    with the demo dogfood wiring. PR-4 just lets the loop survive.

  * ``asyncpg.exceptions.InvalidPasswordError`` → Supavisor evicted
    the custom-role credential cache mid-run. Reset the service pool,
    sleep 1 s, and retry the message once. If the retry also fails,
    drop it and log as error.

  * Any other ``Exception`` → log error, ACK, move on. We never let
    a worker crash on a malformed message; the dispatcher's HMAC +
    schema validation should have caught structural issues before
    enqueue.

Cancellation (``asyncio.CancelledError``) propagates so lifespan
shutdown is clean.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import asyncpg

from polaris_api.db.pools import reset_service_pool
from polaris_api.services import pipeline
from polaris_api.services._llm_models import LLMConfigError, LLMHardFailure
from polaris_api.services.inbound_queue import get_queue

if TYPE_CHECKING:
    from polaris_api.services.pipeline import PipelineDeps

logger = logging.getLogger(__name__)


async def run_worker(deps: PipelineDeps) -> None:
    """Drain the inbound queue forever, dispatching to pipeline.run.

    Caller (main.py lifespan) constructs ``deps`` once + spawns this
    as ``asyncio.create_task(run_worker(deps))``. The task lives
    until cancellation.
    """
    queue = get_queue()
    logger.info("inbound_worker: pipeline consumer started")
    try:
        while True:
            message = await queue.get()
            try:
                # ADR-066: demo-aware entry point — routes the inbound to the
                # Polaris sales bot (default), a brand demo, or a canned
                # activate/expire/refuse message, then falls through to the
                # normal pipeline for default/continue turns.
                decision = await pipeline.run_inbound(incoming=message, deps=deps)
                logger.info(
                    "inbound_worker: tenant=%s conv=%s intent=%s "
                    "model=%s handoff=%s stop_blocked=%s "
                    "quota_exceeded=%s wamid=%s",
                    message.tenant_id,
                    decision.conversation_id,
                    decision.response.intent if decision.response else None,
                    decision.response.model_used if decision.response else None,
                    decision.handoff_emitted,
                    decision.stop_blocked,
                    decision.quota_exceeded,
                    decision.provider_message_id,
                )
            except LLMConfigError as exc:
                logger.warning(
                    "inbound_worker: dropped tenant=%s reason=llm_config_error "
                    "detail=%s — set real ANTHROPIC_API_KEY / OPENROUTER_API_KEY",
                    message.tenant_id, exc,
                )
            except LLMHardFailure as exc:
                logger.error(
                    "inbound_worker: dropped tenant=%s reason=llm_hard_failure "
                    "detail=%s — Phase 6 wires apology-reply path",
                    message.tenant_id, exc,
                )
            except ValueError as exc:
                # provider='chatwoot' (Phase 7 path) or other shape mismatch.
                logger.warning(
                    "inbound_worker: dropped tenant=%s reason=value_error detail=%s",
                    message.tenant_id, exc,
                )
            except asyncpg.exceptions.InvalidPasswordError:
                # Supavisor evicted the custom-role credential cache — the pool's
                # connections are all dead. Reset the service pool and retry the
                # message once. If the retry also fails it falls through to the
                # generic handler below and is dropped (logged as error).
                logger.warning(
                    "inbound_worker: service pool auth failure for tenant=%s "
                    "— resetting pool and retrying once",
                    message.tenant_id,
                )
                try:
                    await reset_service_pool()
                    await asyncio.sleep(1)
                    decision = await pipeline.run_inbound(incoming=message, deps=deps)
                    logger.info(
                        "inbound_worker: retry OK tenant=%s conv=%s",
                        message.tenant_id,
                        decision.conversation_id,
                    )
                except Exception as retry_exc:  # noqa: BLE001
                    logger.error(
                        "inbound_worker: dropped tenant=%s after pool-reset retry: %s",
                        message.tenant_id, retry_exc,
                    )
            except Exception as exc:  # noqa: BLE001
                # Catch-all so a single bad message can't kill the worker.
                # Real fix lands in the dispatcher (validation upstream of
                # enqueue) when the offending shape is identified in logs.
                logger.error(
                    "inbound_worker: dropped tenant=%s reason=%s detail=%s",
                    message.tenant_id, type(exc).__name__, exc,
                )
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        logger.info("inbound_worker: pipeline consumer stopping")
        raise
