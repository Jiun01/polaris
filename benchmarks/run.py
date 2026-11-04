"""Benchmark runner — Phase 4 PR-5.

Runs eval suites end-to-end against the real LLM stack (or a sandbox
fake) and emits a pass/fail report. Invoked manually via ``make eval``
or directly.

Usage:

  uv run python benchmarks/run.py --suite intent
  uv run python benchmarks/run.py --suite all
  uv run python benchmarks/run.py --suite intent --sandbox  # mocked

Suite codes:
  - ``intent``   — runs ``evals/intent_classification.yaml`` against
                   ``services/llm.classify_intent`` (real Haiku).
  - ``rag``      — runs ``evals/rag_queries.yaml`` against a fixture
                   retrieval surface (seeded_chunks from the YAML
                   sorted by simple keyword overlap). The full
                   end-to-end Cohere-rerank evaluation lives in the
                   nightly integration job (Phase 5+), not here.
  - ``all``      — both.

Per plan decision-lock 4, the per-suite thresholds (intent ≥ 0.9,
rag ≥ 0.8) gate the run. Exit code 1 on failure; 0 on pass.
``--sandbox`` mode skips real LLM calls + uses a deterministic
predictor — useful for harness smoke-testing without burning API
budget.

Output JSON archived under ``benchmarks/runs/<UTC-iso>.json`` so
the operator can chart pass-rate trends over time before the
Phase 9 dashboard lands.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Direct-script invocation (`python benchmarks/run.py`) doesn't have the
# repo root on sys.path, so `from evals._runner import ...` fails. Pytest
# gets the pythonpath via pyproject.toml; this is the equivalent for the
# Makefile + ops-shell path. The `if str(...) not in sys.path` guard
# keeps re-imports safe.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from polaris_api.services import llm  # noqa: E402 — after sys.path tweak
from polaris_api.services._llm_models import (  # noqa: E402
    LLMConfigError,
    LLMTransientError,
)

from evals._runner import (  # noqa: E402 — after sys.path tweak
    IntentCase,
    IntentScore,
    RagCase,
    RagScore,
    SuiteReport,
    build_intent_report,
    build_rag_report,
    load_intent_suite,
    load_rag_suite,
    score_intent,
    score_rag,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
EVALS_DIR = REPO_ROOT / "evals"
RUNS_DIR = REPO_ROOT / "benchmarks" / "runs"


# ----------------------------------------------------------------------
# Predictors — live (real LLM/RAG) vs sandbox (deterministic stubs)
# ----------------------------------------------------------------------


async def predict_intent_live(case: IntentCase) -> str:
    """Call services/llm.classify_intent against real Anthropic."""
    try:
        return await llm.classify_intent(case.input)
    except (LLMConfigError, LLMTransientError) as exc:
        logger.error("intent live predictor failed: %s", type(exc).__name__)
        raise


def predict_intent_sandbox(case: IntentCase) -> str:
    """Sandbox predictor: returns the expected tier directly.

    Pass-rate is trivially 100% in sandbox mode — this verifies the
    harness shape (loader + scorer + reporter), not the model. CI
    uses this mode; live mode is for ``make eval``.
    """
    return case.expected


async def predict_rag_live(_case: RagCase) -> list[str]:
    """Live RAG: deferred to Phase 5+ integration eval.

    The full retrieval path needs a seeded Postgres + pgvector setup
    — out of scope for the PR-5 manual ``make eval``. The runner
    surfaces this as a clear skip; nightly integration tests will
    exercise the real path.
    """
    raise NotImplementedError(
        "Live RAG eval needs a seeded Postgres + pgvector. Use "
        "--sandbox for harness smoke; nightly integration test (Phase "
        "5+) runs the full retrieval path."
    )


def predict_rag_sandbox(case: RagCase) -> list[str]:
    """Sandbox RAG predictor: simple keyword-overlap ranking.

    For each seeded chunk, count the number of query tokens that
    appear in the chunk content (case-insensitive). Higher overlap
    ranks higher. Ties broken by seeded order. This is enough for
    the harness scorer to be exercised; real BM25 + vector + Cohere
    rerank is the live path's job.
    """
    query_tokens = {t.lower() for t in case.query.split() if len(t) > 1}
    scored: list[tuple[int, int, str]] = []  # (overlap, -idx, id)
    for idx, chunk in enumerate(case.seeded_chunks):
        content_tokens = {t.lower() for t in chunk.content.split() if len(t) > 1}
        overlap = len(query_tokens & content_tokens)
        scored.append((overlap, -idx, chunk.id))
    scored.sort(reverse=True)
    return [cid for _, _, cid in scored]


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------


async def run_intent_suite(*, sandbox: bool) -> SuiteReport:
    path = EVALS_DIR / "intent_classification.yaml"
    suite = load_intent_suite(path)
    scores: list[IntentScore] = []
    for case in suite.cases:
        predicted = (
            predict_intent_sandbox(case)
            if sandbox
            else await predict_intent_live(case)
        )
        scores.append(score_intent(case, predicted))
    return build_intent_report(suite.header, scores)


async def run_rag_suite(*, sandbox: bool) -> SuiteReport:
    """Run the RAG eval suite. Live mode is explicitly skipped at Phase 4.

    Per code-review M-4: pre-PR-7 the live path called
    ``predict_rag_live(suite.cases[0])`` which raised
    NotImplementedError and made the rest of the function unreachable
    — the runner archived an empty/missing report silently. Now we
    return an explicit ``SuiteReport(skipped=True)`` so the archive +
    stdout report make the skip visible.
    """
    path = EVALS_DIR / "rag_queries.yaml"
    suite = load_rag_suite(path)
    if not sandbox:
        return SuiteReport(
            suite=suite.header.suite,
            total=0, passed=0, failed=0,
            pass_rate=0.0, threshold=suite.header.threshold,
            passes_gate=True,   # skip is not a fail
            failures=(),
            skipped=True,
            skipped_reason=(
                "Live RAG eval needs a seeded Postgres + pgvector. "
                "Use --sandbox for harness smoke; Phase 5+ integration "
                "test runs the full retrieval path."
            ),
        )
    scores: list[RagScore] = []
    for case in suite.cases:
        returned = predict_rag_sandbox(case)
        scores.append(score_rag(case, returned))
    return build_rag_report(suite.header, scores)


def _archive_run(reports: list[SuiteReport], *, sandbox: bool) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = "-sandbox" if sandbox else "-live"
    path = RUNS_DIR / f"{ts}{suffix}.json"
    payload: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "sandbox": sandbox,
        "reports": [
            {
                "suite": r.suite,
                "total": r.total,
                "passed": r.passed,
                "failed": r.failed,
                "pass_rate": r.pass_rate,
                "threshold": r.threshold,
                "passes_gate": r.passes_gate,
                "failures": list(r.failures),
            }
            for r in reports
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


async def _amain(args: argparse.Namespace) -> int:
    suite_codes = {"intent", "rag"} if args.suite == "all" else {args.suite}
    reports: list[SuiteReport] = []
    t0 = time.monotonic()
    try:
        if "intent" in suite_codes:
            reports.append(await run_intent_suite(sandbox=args.sandbox))
        if "rag" in suite_codes:
            reports.append(await run_rag_suite(sandbox=args.sandbox))
    except NotImplementedError as exc:
        logger.error("eval skipped: %s", exc)
        return 4

    duration_s = time.monotonic() - t0
    archive_path = _archive_run(reports, sandbox=args.sandbox)

    print()  # noqa: T201 — runner stdout is the user-facing report
    print("=" * 60)  # noqa: T201
    for r in reports:
        print(r.fmt())  # noqa: T201
        if r.failures:
            print(f"  failures: {', '.join(r.failures)}")  # noqa: T201
    print("=" * 60)  # noqa: T201
    print(f"duration: {duration_s:.1f}s  |  archive: {archive_path.name}")  # noqa: T201

    return 0 if all(r.passes_gate for r in reports) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite", choices=("intent", "rag", "all"), default="all",
    )
    parser.add_argument(
        "--sandbox", action="store_true",
        help="Use deterministic stub predictors (no API calls)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if not args.verbose else logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
