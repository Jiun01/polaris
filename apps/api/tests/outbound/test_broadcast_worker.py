"""Broadcast worker module-level tests — Phase 6 PR-6.

Schema + structure coverage that doesn't need a live DB/Redis. The
full drain-loop integration (Postgres + Redis + meta_whatsapp
stub) runs in CI's migration-check workflow.

Coverage at PR-6:
  * Public surface (run_forever) is importable + the only export.
  * Queue-key helper resolves consistently with broadcast/service —
    if these drift, the worker scans an empty key while the service
    ZADDs to a different one.
  * env-int helper falls back cleanly on missing / malformed values.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from polaris_api.services.broadcast import worker


def test_run_forever_is_public_surface() -> None:
    """__all__ pins the public surface so a refactor that exports
    internal helpers (e.g. _claim_and_load) requires an explicit
    decision."""
    assert worker.__all__ == ["run_forever"]
    assert callable(worker.run_forever)


def test_queue_key_matches_service_module() -> None:
    """The worker imports _queue_key from service.py and references
    it; if the helper ever forks (worker building its own key
    locally) drains break silently. Pin the cross-module equality."""
    from polaris_api.services.broadcast import service

    tid = uuid4()
    assert worker._queue_key(tid) == service._queue_key(tid)  # noqa: SLF001


def test_env_int_uses_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROADCAST_TICK_SECONDS", raising=False)
    assert worker._env_int("BROADCAST_TICK_SECONDS", 7) == 7  # noqa: SLF001


def test_env_int_uses_default_when_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROADCAST_TICK_SECONDS", "not-an-int")
    assert worker._env_int("BROADCAST_TICK_SECONDS", 7) == 7  # noqa: SLF001


def test_env_int_parses_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROADCAST_TICK_SECONDS", "42")
    assert worker._env_int("BROADCAST_TICK_SECONDS", 7) == 42  # noqa: SLF001


def test_lint_allowlist_includes_worker() -> None:
    """Pin the lint_service_pool ALLOWED_PREFIXES entry for the
    worker — removing it would break broadcast worker boot under
    the test_pools_lint guard."""
    from apps.api.scripts.lint_service_pool import (
        ALLOWED_PREFIXES,  # type: ignore[import-not-found]
    )

    # The lint_service_pool path normalisation uses forward slashes
    # even on Windows.
    assert "services/broadcast/worker.py" in ALLOWED_PREFIXES
