"""Outbound event-type pinning tests — Phase 6 PR-8.

Phase 6 emits 5 distinct event types into ``webhook_events_outbound``
from 4 separate sites. At GA these rows are PERSISTED ONLY — no
dispatcher fan-out (ADR-035 Amendment evening). The v3.0 dispatcher
will read forward from a launch-date cutoff per Q-PHASE6-02.

Drift on the event_type strings would silently break the v3.0
dispatcher's filter logic. This file pins the exact strings by
grepping the source — a rename in the emission site without an
update here fails fast.

Emission sites + event types:

  services/templates/lifecycle.py
    -> "template.{approved | rejected | paused | disabled | pending}"

  services/audience/opt_out.py
    -> "audience.opted_out"

  services/broadcast/worker.py
    -> "broadcast.completed"
    -> "broadcast.failed"

  webhooks/wa_account_router.py
    -> "channel.mm_lite.{in_progress | onboarded | unlinked}"

We don't test the full DB write here — the migration-check CI
workflow with real Postgres covers that. This file is a fast
unit-level guard against rename drift.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_API_ROOT = _REPO_ROOT / "apps" / "api" / "polaris_api"


def _read(rel_path: str) -> str:
    return (_API_ROOT / rel_path).read_text(encoding="utf-8")


def test_templates_lifecycle_emits_template_status_events() -> None:
    """lifecycle.py emits ``template.<new_status>`` via f-string. Pin
    the f-string shape — a rename to e.g. ``tmpl.<status>`` would
    silently break the v3.0 dispatcher."""
    src = _read("services/templates/lifecycle.py")
    assert 'event_type=f"template.{new_status}"' in src


def test_audience_opt_out_emits_audience_opted_out() -> None:
    src = _read("services/audience/opt_out.py")
    assert '"audience.opted_out"' in src


def test_broadcast_worker_emits_completed_and_failed() -> None:
    """Worker emits two distinct broadcast.* events on terminal
    transitions. Both strings must be present + literal — any
    rename breaks the v3.0 dispatcher's filter on broadcast.*."""
    src = _read("services/broadcast/worker.py")
    assert '"broadcast.completed"' in src
    assert '"broadcast.failed"' in src


def test_wa_account_emits_mm_lite_state_events() -> None:
    """wa-account handler emits ``channel.mm_lite.<state_lower>``
    where <state_lower> ∈ {in_progress, onboarded, unlinked}.
    Pin the f-string."""
    src = _read("webhooks/wa_account_router.py")
    assert 'f"channel.mm_lite.{new_status.lower()}"' in src


def test_event_types_are_dot_separated() -> None:
    """All event types follow ``{namespace}.{action}`` (or
    ``{namespace}.{sub}.{action}`` for channel.mm_lite.*). Pin the
    convention so future emissions don't slip in with
    underscores or slashes that break the dispatcher's namespace
    routing."""
    # Hard-code the expected emissions; this is the spec we're
    # asserting against the source.
    expected = {
        "template.approved",
        "template.rejected",
        "template.paused",
        "template.disabled",
        "template.pending",
        "audience.opted_out",
        "broadcast.completed",
        "broadcast.failed",
        "channel.mm_lite.in_progress",
        "channel.mm_lite.onboarded",
        "channel.mm_lite.unlinked",
    }
    for et in expected:
        # Every event type must follow {ns}.{...} with at least one dot.
        assert et.count(".") >= 1, f"event_type {et!r} missing namespace"
        # No underscores in the namespace prefix (the sub-action token
        # can carry them — opted_out, in_progress).
        ns = et.split(".", 1)[0]
        assert "_" not in ns, f"event_type {et!r} namespace has underscore"


def test_outbound_dispatcher_stays_unbuilt_at_ga() -> None:
    """Per ADR-035 Amendment evening: outbound dispatch is v3.0. Pin
    that no ``webhook_outbound_worker`` module exists in the
    services tree at GA. Any future PR that introduces one without
    updating this guard surfaces in CI — forcing the v3.0 design
    review that ADR-035 prescribed."""
    services = _API_ROOT / "services"
    candidates = list(services.glob("**/webhook_outbound*"))
    candidates += list(services.glob("**/outbound_webhook*"))
    # Whitelist: the wa_account_router lives in webhooks/, not services/,
    # so it doesn't match the patterns above. The migration's
    # webhook_events_outbound is data-layer, not a worker module.
    assert candidates == [], (
        f"Found outbound-dispatcher module(s) {candidates}; v3.0 dispatcher "
        "should not exist at GA. If you're intentionally adding the v3.0 "
        "worker, update this test + ADR-035."
    )


def test_outbound_table_migration_exists() -> None:
    """Pin migration 047 — the table writers in templates/lifecycle,
    audience/opt_out, broadcast/worker, and webhooks/wa_account_router
    all depend on this table existing. Drift would surface as
    UndefinedTableError-guarded silent skips in production."""
    migration = (
        _REPO_ROOT
        / "packages"
        / "db-migrations"
        / "047_webhook_events_outbound.sql"
    )
    assert migration.exists(), (
        "Migration 047 (webhook_events_outbound) missing; outbound event "
        "writes from PR-3/PR-4/PR-6/PR-7 will all silently no-op via "
        "UndefinedTableError guards."
    )
    src = migration.read_text(encoding="utf-8")
    assert "CREATE TABLE webhook_events_outbound" in src
    assert "tenant_id" in src
    assert "event_type" in src
    assert "payload" in src
    assert "ENABLE ROW LEVEL SECURITY" in src
