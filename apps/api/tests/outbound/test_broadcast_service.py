"""Broadcast service module-level tests — Phase 6 PR-5.

Pure structural coverage: typed errors exist, default queue
namespace matches env contract, Redis key helpers shape correctly.
Full schedule->pause->resume->cancel lifecycle against real DB is
exercised by the migration-check CI workflow.
"""

from __future__ import annotations

from uuid import uuid4

from polaris_api.services.broadcast import service


def test_typed_error_hierarchy_subclasses_base() -> None:
    """Router error mapper relies on isinstance() checks across the
    BroadcastError subclass tree. Pin the hierarchy so a future
    refactor that flattens the tree doesn't break router 4xx
    mapping."""
    for sub in (
        service.BroadcastTierBlockedError,
        service.BroadcastTemplateNotApprovedError,
        service.BroadcastAudienceEmptyError,
        service.BroadcastQuotaExceededError,
        service.BroadcastStateInvalidError,
    ):
        assert issubclass(sub, service.BroadcastError)
    # And BroadcastError itself is a RuntimeError so generic except
    # handlers further up the stack catch it.
    assert issubclass(service.BroadcastError, RuntimeError)


def test_queue_key_uses_namespace_prefix() -> None:
    """Tenant ZSET key is ``{namespace}:queue:{tenant_id}``. Pin the
    shape — the worker (PR-6) builds the same key via the same helper,
    so a drift here would cause the worker to scan an empty key while
    schedule_broadcast ZADDs to a different one."""
    tid = uuid4()
    key = service._queue_key(tid)  # noqa: SLF001
    assert key.endswith(f":queue:{tid}")


def test_recipients_key_uses_broadcast_id() -> None:
    bid = uuid4()
    key = service._recipients_key(bid)  # noqa: SLF001
    assert key.endswith(f":recipients:{bid}")


def test_default_queue_namespace_matches_env_doc() -> None:
    """Default namespace 'broadcast' must match what .env.example
    documents as BROADCAST_QUEUE_NAMESPACE. Drift makes local-dev
    workers + API processes use different keys."""
    assert service._DEFAULT_QUEUE_NAMESPACE == "broadcast"  # noqa: SLF001
