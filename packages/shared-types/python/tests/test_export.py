"""Tests for the Pydantic -> JSON Schema export pipeline.

PR-1: model_json_schema() basic shape.
PR-4: end-to-end build_schema(), schema_text() determinism, and the --check
drift gate.
audit-batch-f-frontend-consolidation (2026-07): regression coverage for a
bundling bug found while unblocking the TS codegen pipeline -- see
build_schema()'s docstring in export.py for the full mechanics.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from polaris_shared_types.example import HealthStatus

# Resolve repo paths
ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "packages" / "shared-types" / "scripts" / "export.py"
SCHEMA_JSON = ROOT / "packages" / "shared-types" / "typescript" / "schema.json"


def test_health_status_schema_has_expected_fields() -> None:
    schema = HealthStatus.model_json_schema()
    props = schema["properties"]
    assert set(props.keys()) == {"status", "supabase", "redis", "version"}
    # Literal types serialize as an inline `enum` in Pydantic v2 JSON Schema
    # (used to be `string` + `pattern`). The TS generator turns this into
    # discriminated string-union types instead of plain `string`.
    assert props["status"]["enum"] == ["ok", "degraded", "down"]
    assert props["supabase"]["enum"] == ["reachable", "unreachable"]
    assert props["redis"]["enum"] == ["reachable", "unreachable"]


def test_health_status_validates_known_shape() -> None:
    body = HealthStatus(
        status="ok",
        supabase="reachable",
        redis="reachable",
        version="a1b2c3d",
    )
    assert body.model_dump() == {
        "status": "ok",
        "supabase": "reachable",
        "redis": "reachable",
        "version": "a1b2c3d",
    }


def test_build_schema_is_deterministic() -> None:
    """Two calls produce identical sorted-key JSON output."""
    from polaris_shared_types import HealthStatus as _HS  # noqa: F401 — re-import is intentional

    # Import via module path the script uses
    sys.path.insert(0, str(ROOT / "packages" / "shared-types" / "scripts"))
    try:
        import export  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    a = json.dumps(export.build_schema(), indent=2, sort_keys=True)
    b = json.dumps(export.build_schema(), indent=2, sort_keys=True)
    assert a == b
    parsed = json.loads(a)
    assert parsed["title"] == "PolarisSharedTypes"
    assert "HealthStatus" in parsed["$defs"]


def test_check_drift_passes_on_committed_schema() -> None:
    """The committed schema.json must match the current Pydantic source."""
    proc = subprocess.run(  # noqa: S603 — fixed argv
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"export.py --check reported drift.\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}\n"
        f"Run `make export-types` and commit the regenerated schema.json."
    )


@pytest.mark.skipif(not SCHEMA_JSON.exists(), reason="schema.json not yet generated")
def test_committed_schema_json_has_health_status() -> None:
    """Sanity check: the committed file actually contains HealthStatus."""
    parsed = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    assert "HealthStatus" in parsed["$defs"]
    assert parsed["$defs"]["HealthStatus"]["type"] == "object"


def test_build_schema_has_no_dangling_refs() -> None:
    """Regression test: every `$ref: "#/$defs/X"` must resolve to a
    top-level `$defs` key.

    build_schema() originally nested each model's own model_json_schema()
    output directly under `$defs.<Model>`, burying that model's OWN nested
    `$defs` (enums, sub-models) one level too deep at
    `$defs.<Model>.$defs.<Nested>` while the model's `$ref` strings still
    read `#/$defs/<Nested>` -- correct relative to the model's own
    document root, but dangling once combined into the shared multi-model
    schema (58 dangling refs across the real model set, discovered while
    unblocking the json-schema-to-typescript generator, which correctly
    refuses to run against a schema with unresolvable refs rather than
    emit wrong output). Any model with a nested enum or sub-model
    reproduces this if the hoisting in build_schema() regresses.
    """
    sys.path.insert(0, str(ROOT / "packages" / "shared-types" / "scripts"))
    try:
        import export  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    schema = export.build_schema()
    top_level_defs = set(schema["$defs"].keys())
    text = json.dumps(schema)
    ref_targets = set(re.findall(r'"\$ref":\s*"#/\$defs/([A-Za-z0-9_]+)"', text))

    dangling = ref_targets - top_level_defs
    assert not dangling, (
        f"Dangling $ref target(s) with no top-level $defs entry: "
        f"{sorted(dangling)}. This means at least one model's nested type "
        f"(enum or sub-model) failed to hoist to the shared $defs -- "
        f"json-schema-to-typescript will raise MissingPointerError on "
        f"this schema. See build_schema()'s docstring in export.py."
    )

    # No model's own schema should retain a nested $defs block after
    # hoisting -- the whole point is that they've all moved to the
    # top level.
    for name, model_schema in schema["$defs"].items():
        assert "$defs" not in model_schema, (
            f"{name} still has its own nested $defs block -- "
            "build_schema() should have hoisted it to the top level."
        )
