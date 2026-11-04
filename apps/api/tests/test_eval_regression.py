"""Eval harness regression test — Phase 4 PR-5.

CI-side guards on the eval harness itself. Does NOT call real LLM —
that's `make eval` (manual, see benchmarks/run.py). What this DOES:

  * Validates that ``evals/*.yaml`` parse cleanly through the
    runner's schema-checking loader. A typo (bad tier label,
    missing field) fails CI loudly, before a nightly eval run dies
    with the same error.

  * Asserts minimum case counts per Phase 4 exit criterion 2:
    "Eval suite passes regression cases — 15+ intent examples,
    10+ RAG queries".

  * Smoke-tests the scoring helpers + sandbox predictors so a
    refactor of ``_runner.py`` can't silently break the report
    shape that ``benchmarks/run.py`` (and Phase 9 dashboard later)
    depends on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Repo root: tests/ → api/ → apps/ → repo
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_EVALS_DIR = _REPO_ROOT / "evals"


def test_intent_yaml_parses_cleanly() -> None:
    """evals/intent_classification.yaml round-trips through the loader."""
    from evals._runner import load_intent_suite

    suite = load_intent_suite(_EVALS_DIR / "intent_classification.yaml")
    assert suite.header.version == 1
    assert suite.header.suite == "intent_classification"
    assert 0.0 < suite.header.threshold <= 1.0


def test_rag_yaml_parses_cleanly() -> None:
    """evals/rag_queries.yaml round-trips through the loader."""
    from evals._runner import load_rag_suite

    suite = load_rag_suite(_EVALS_DIR / "rag_queries.yaml")
    assert suite.header.version == 1
    assert suite.header.suite == "rag_queries"
    assert 0.0 < suite.header.threshold <= 1.0


def test_intent_corpus_meets_phase_4_minimum() -> None:
    """Phase 4 exit criterion 2: at least 15 intent classification cases.

    Per deep-dive §10.2 / phase-04 checklist PR-5: the regression
    suite must cover 15+ intent examples + 10+ RAG queries.
    """
    from evals._runner import load_intent_suite

    suite = load_intent_suite(_EVALS_DIR / "intent_classification.yaml")
    assert len(suite.cases) >= 15, (
        "intent_classification.yaml has fewer than 15 cases — "
        "Phase 4 exit criterion 2 not met"
    )


def test_rag_corpus_meets_phase_4_minimum() -> None:
    """Phase 4 exit criterion 2: at least 10 RAG queries."""
    from evals._runner import load_rag_suite

    suite = load_rag_suite(_EVALS_DIR / "rag_queries.yaml")
    assert len(suite.cases) >= 10, (
        "rag_queries.yaml has fewer than 10 cases — "
        "Phase 4 exit criterion 2 not met"
    )


def test_intent_suite_covers_all_four_tiers() -> None:
    """At least one example per tier (simple, medium, hard, handoff)."""
    from evals._runner import load_intent_suite

    suite = load_intent_suite(_EVALS_DIR / "intent_classification.yaml")
    tiers_present = {c.expected for c in suite.cases}
    assert tiers_present == {"simple", "medium", "hard", "handoff"}, (
        f"intent_classification.yaml missing tiers: "
        f"{sorted({'simple', 'medium', 'hard', 'handoff'} - tiers_present)}"
    )


def test_intent_suite_covers_all_three_languages() -> None:
    """At least one example per language (en, ms, zh-cn)."""
    from evals._runner import load_intent_suite

    suite = load_intent_suite(_EVALS_DIR / "intent_classification.yaml")
    langs_present = {c.language for c in suite.cases}
    assert langs_present == {"en", "ms", "zh-cn"}, (
        f"intent_classification.yaml missing languages: "
        f"{sorted({'en', 'ms', 'zh-cn'} - langs_present)}"
    )


def test_rag_case_ids_unique() -> None:
    """No duplicate rag case ids (loader doesn't enforce — caught here)."""
    from evals._runner import load_rag_suite

    suite = load_rag_suite(_EVALS_DIR / "rag_queries.yaml")
    ids = [c.id for c in suite.cases]
    assert len(ids) == len(set(ids)), "duplicate rag case ids"


def test_intent_case_ids_unique() -> None:
    from evals._runner import load_intent_suite

    suite = load_intent_suite(_EVALS_DIR / "intent_classification.yaml")
    ids = [c.id for c in suite.cases]
    assert len(ids) == len(set(ids)), "duplicate intent case ids"


def test_intent_scorer_correct_match() -> None:
    """score_intent matches case-insensitive + whitespace-stripped."""
    from evals._runner import IntentCase, score_intent

    case = IntentCase(id="x", input="hi", expected="simple", language="en")
    assert score_intent(case, "simple").correct is True
    assert score_intent(case, " SIMPLE\n").correct is True
    assert score_intent(case, "medium").correct is False
    assert score_intent(case, "").correct is False


def test_rag_scorer_top_1_only() -> None:
    """score_rag scores 1 iff expected_top_chunk == returned_ids[0]."""
    from evals._runner import RagCase, SeededChunk, score_rag

    case = RagCase(
        id="x", query="q",
        seeded_chunks=(
            SeededChunk(id="a", content="x", source="s"),
            SeededChunk(id="b", content="y", source="s"),
        ),
        expected_top_chunk="a",
    )
    assert score_rag(case, ["a", "b"]).correct is True
    assert score_rag(case, ["b", "a"]).correct is False
    assert score_rag(case, []).correct is False


def test_report_passes_gate_only_above_threshold() -> None:
    """SuiteReport.passes_gate uses >=, not > (boundary inclusive)."""
    from evals._runner import (
        IntentCase,
        IntentScore,
        SuiteHeader,
        build_intent_report,
    )

    header = SuiteHeader(version=1, suite="x", threshold=0.5)
    cases = [
        IntentCase(id=str(i), input="x", expected="simple", language="en")
        for i in range(10)
    ]
    # 5/10 = 0.5 — exactly at threshold, should pass.
    scores = [
        IntentScore(
            case_id=c.id, expected="simple",
            predicted="simple" if i < 5 else "medium",
            correct=i < 5,
        )
        for i, c in enumerate(cases)
    ]
    report = build_intent_report(header, scores)
    assert report.pass_rate == 0.5
    assert report.passes_gate is True


def test_sandbox_intent_run_is_trivially_100_percent() -> None:
    """Sandbox predictor returns expected label → 100% pass rate.

    Smoke-tests the full harness chain (load → predict → score →
    report) end-to-end without an event loop dance, since the
    sandbox predictor is synchronous.
    """
    from benchmarks.run import predict_intent_sandbox
    from evals._runner import (
        build_intent_report,
        load_intent_suite,
        score_intent,
    )

    suite = load_intent_suite(_EVALS_DIR / "intent_classification.yaml")
    scores = [
        score_intent(c, predict_intent_sandbox(c))
        for c in suite.cases
    ]
    report = build_intent_report(suite.header, scores)
    assert report.passes_gate is True
    assert report.pass_rate == 1.0
    assert report.failed == 0


def test_sandbox_rag_predictor_runs_without_error() -> None:
    """Sandbox keyword-overlap predictor returns a chunk id list for every case."""
    from benchmarks.run import predict_rag_sandbox
    from evals._runner import load_rag_suite

    suite = load_rag_suite(_EVALS_DIR / "rag_queries.yaml")
    for case in suite.cases:
        returned = predict_rag_sandbox(case)
        # All seeded chunks are returned (just reordered).
        assert set(returned) == {c.id for c in case.seeded_chunks}


def test_bad_intent_yaml_raises_clear_error(tmp_path: Path) -> None:
    """Malformed YAML surfaces a useful error, not a KeyError trace."""
    from evals._runner import load_intent_suite

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "version: 1\nsuite: intent_classification\nthreshold: 0.9\n"
        "cases:\n"
        "  - id: bad-1\n"
        "    input: hello\n"
        "    expected: not-a-real-tier\n"  # invalid label
        "    language: en\n"
    )
    with pytest.raises(ValueError, match="expected must be one of"):
        load_intent_suite(bad)


def test_bad_rag_case_unsatisfiable_chunk_caught(tmp_path: Path) -> None:
    """expected_top_chunk must be in seeded_chunks (else unsatisfiable)."""
    from evals._runner import load_rag_suite

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "version: 1\nsuite: rag_queries\nthreshold: 0.8\n"
        "cases:\n"
        "  - id: bad-1\n"
        "    query: q\n"
        "    seeded_chunks:\n"
        "      - id: chunk-x\n"
        "        content: hello\n"
        "        source: f.md\n"
        "    expected_top_chunk: nonexistent\n"
    )
    with pytest.raises(ValueError, match="unsatisfiable"):
        load_rag_suite(bad)
