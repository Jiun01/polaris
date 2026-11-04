"""Wiring-pin tests — Knowledge Gap Detection / M1.

These pin the capture integration so it can't be removed/renamed silently.
Full behavioural capture (real DB) is covered by the operator/E2E smoke pass,
consistent with how the repo defers full pipeline.run integration.
"""

from __future__ import annotations


def test_pipeline_imports_capture() -> None:
    from polaris_api.services import pipeline

    assert hasattr(pipeline, "knowledge_gap_capture")


def test_capture_gate_setting_exists() -> None:
    from polaris_api.config import get_settings

    settings = get_settings()
    assert hasattr(settings, "knowledge_gap_capture_enabled")
    assert hasattr(settings, "knowledge_gap_retrieval_threshold")
    # Ultra-gate set: capture + dashboard both gate on this.
    assert settings.knowledge_gap_tiers == frozenset({"ultra", "enterprise"})


def test_pipeline_resolves_capture_tier_gate() -> None:
    # Pin that pipeline computes the Ultra capture gate (capture_ok) rather than
    # capturing for every tier — guards against the gate being dropped.
    #
    # audit Batch B (B1): the gate now reads plan_tier off the consolidated
    # tenant snapshot loaded once near the top of _run_inner, via the pure
    # plan_tier_in_capture_tier() helper, instead of issuing its own
    # `SELECT plan_tier FROM tenants` via tenant_in_capture_tier(). The
    # DB-backed tenant_in_capture_tier() still exists in
    # knowledge_gaps/capture.py for other callers/tests.
    import inspect

    from polaris_api.services import pipeline

    # The pipeline body lives in _run_inner (run() is a thin wrapper that flushes
    # deferred Chatwoot side-effects after the transaction commits).
    src = inspect.getsource(pipeline._run_inner)
    assert "capture_ok" in src
    assert "plan_tier_in_capture_tier" in src
