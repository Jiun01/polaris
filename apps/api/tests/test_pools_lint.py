"""CI lint regression — `get_service_pool` allowlist gate.

Runs `apps/api/scripts/lint_service_pool.py` and asserts it exits 0 against
the current tree. Also asserts that a synthetic violation (a temp file
referencing `get_service_pool` outside the allowlist) trips the gate — so the
lint logic itself stays alive even if the codebase never violates it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Module-level script path: deterministic across pytest invocations.
_LINT_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "lint_service_pool.py"
)


def test_lint_script_exists() -> None:
    assert _LINT_SCRIPT.exists(), f"lint script missing at {_LINT_SCRIPT}"


def test_current_tree_passes_lint() -> None:
    """Real codebase should be clean — no `get_service_pool` outside allowlist."""
    result = subprocess.run(  # noqa: S603 — script path is constant
        [sys.executable, str(_LINT_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"lint_service_pool reported violations:\nSTDOUT: {result.stdout}\n"
        f"STDERR: {result.stderr}"
    )


def test_synthetic_violation_is_caught(tmp_path: Path) -> None:
    """Drop a fake module that imports `get_service_pool` outside the allowlist
    and run the lint against a copy of the tree. The gate must fire.

    Implementation: stage a minimal tree under tmp_path that the lint can
    walk. Reuse the real lint module's `_scan` function so we exercise the
    actual logic, not a re-implementation.
    """
    sys.path.insert(0, str(_LINT_SCRIPT.parent))
    try:
        # Import dynamically; the module is a CLI script (no package).
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "lint_service_pool_under_test", _LINT_SCRIPT
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(_LINT_SCRIPT.parent))

    # Build a tiny fake `polaris_api` tree under tmp_path that mirrors a banned
    # import in a non-allowlisted directory (`routers/`).
    fake_pkg = tmp_path / "polaris_api"
    (fake_pkg / "routers").mkdir(parents=True)
    (fake_pkg / "routers" / "bad_route.py").write_text(
        "from polaris_api.db.pools import get_service_pool\n", encoding="utf-8"
    )
    # Also drop a sanctioned use to confirm the allowlist works in both directions.
    (fake_pkg / "services" / "admin").mkdir(parents=True)
    (fake_pkg / "services" / "admin" / "ops.py").write_text(
        "from polaris_api.db.pools import get_service_pool\n", encoding="utf-8"
    )

    hits = mod._scan(fake_pkg)  # noqa: SLF001 — test-only access to the scanner
    rels = sorted(h[0].relative_to(fake_pkg).as_posix() for h in hits)
    assert rels == ["routers/bad_route.py"], (
        f"expected the routers/ violation only, got: {rels}"
    )
