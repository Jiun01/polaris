"""Shared eval-suite loader + scorer — Phase 4 PR-5.

Two suites at GA (per deep-dive §8.4):

  * ``intent_classification`` — Haiku classifier pass-rate.
  * ``rag_queries`` — top-1 recall on hybrid retrieval.

Each suite is one YAML file (see ``_schema.md``). This module parses
the YAMLs into typed dataclasses + offers scoring helpers. The
actual LLM/RAG invocation lives in ``benchmarks/run.py`` (manual
``make eval``) and ``tests/test_eval_regression.py`` (sandbox CI).

Why dataclasses (not Pydantic) here: the eval harness shouldn't pay
the Pydantic import + validation cost on every CI run that just
loads the YAML; the schema is internal + the structure is checked
by ``test_eval_regression.py`` at load time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

IntentLabel = Literal["simple", "medium", "hard", "handoff"]
LanguageLabel = Literal["en", "ms", "zh-cn"]


# ----------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class IntentCase:
    id: str
    input: str
    expected: IntentLabel
    language: LanguageLabel


@dataclass(slots=True, frozen=True)
class SeededChunk:
    id: str
    content: str
    source: str


@dataclass(slots=True, frozen=True)
class RagCase:
    id: str
    query: str
    seeded_chunks: tuple[SeededChunk, ...]
    expected_top_chunk: str


@dataclass(slots=True, frozen=True)
class SuiteHeader:
    version: int
    suite: str
    threshold: float


@dataclass(slots=True)
class IntentSuite:
    header: SuiteHeader
    cases: list[IntentCase] = field(default_factory=list)


@dataclass(slots=True)
class RagSuite:
    header: SuiteHeader
    cases: list[RagCase] = field(default_factory=list)


# ----------------------------------------------------------------------
# Loaders
# ----------------------------------------------------------------------


def load_intent_suite(path: Path) -> IntentSuite:
    raw = _load_yaml(path)
    header = _parse_header(raw, suite_name="intent_classification")
    cases = [_parse_intent_case(c, idx) for idx, c in enumerate(raw["cases"])]
    return IntentSuite(header=header, cases=cases)


def load_rag_suite(path: Path) -> RagSuite:
    raw = _load_yaml(path)
    header = _parse_header(raw, suite_name="rag_queries")
    cases = [_parse_rag_case(c, idx) for idx, c in enumerate(raw["cases"])]
    return RagSuite(header=header, cases=cases)


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class IntentScore:
    case_id: str
    expected: IntentLabel
    predicted: str
    correct: bool


@dataclass(slots=True, frozen=True)
class RagScore:
    case_id: str
    expected_top_chunk: str
    returned_ids: tuple[str, ...]
    correct: bool


@dataclass(slots=True, frozen=True)
class SuiteReport:
    suite: str
    total: int
    passed: int
    failed: int
    pass_rate: float
    threshold: float
    passes_gate: bool
    failures: tuple[str, ...]  # case ids of failures
    # Per code-review M-4: explicit "skipped" state so callers can
    # distinguish "0/0 passed because the suite was deliberately
    # skipped" from "0/0 passed because nothing ran". skipped=True
    # implies passes_gate=True (skip is not a fail) and total=0.
    skipped: bool = False
    skipped_reason: str | None = None

    def fmt(self) -> str:
        if self.skipped:
            return f"[SKIP] {self.suite}: {self.skipped_reason or 'no reason given'}"
        gate = "PASS" if self.passes_gate else "FAIL"
        return (
            f"[{gate}] {self.suite}: "
            f"{self.passed}/{self.total} = {self.pass_rate:.1%} "
            f"(threshold {self.threshold:.1%})"
        )


def score_intent(case: IntentCase, predicted: str) -> IntentScore:
    """Case-insensitive whitespace-stripped match against expected tier."""
    norm = (predicted or "").strip().lower()
    return IntentScore(
        case_id=case.id,
        expected=case.expected,
        predicted=norm,
        correct=norm == case.expected,
    )


def score_rag(case: RagCase, returned_ids: list[str]) -> RagScore:
    """Top-1 recall: expected chunk must be at index 0."""
    top = returned_ids[0] if returned_ids else ""
    return RagScore(
        case_id=case.id,
        expected_top_chunk=case.expected_top_chunk,
        returned_ids=tuple(returned_ids),
        correct=top == case.expected_top_chunk,
    )


def build_intent_report(
    header: SuiteHeader, scores: list[IntentScore],
) -> SuiteReport:
    total = len(scores)
    passed = sum(1 for s in scores if s.correct)
    failed = total - passed
    rate = passed / total if total else 0.0
    return SuiteReport(
        suite=header.suite,
        total=total, passed=passed, failed=failed,
        pass_rate=rate, threshold=header.threshold,
        passes_gate=rate >= header.threshold,
        failures=tuple(s.case_id for s in scores if not s.correct),
    )


def build_rag_report(
    header: SuiteHeader, scores: list[RagScore],
) -> SuiteReport:
    total = len(scores)
    passed = sum(1 for s in scores if s.correct)
    failed = total - passed
    rate = passed / total if total else 0.0
    return SuiteReport(
        suite=header.suite,
        total=total, passed=passed, failed=failed,
        pass_rate=rate, threshold=header.threshold,
        passes_gate=rate >= header.threshold,
        failures=tuple(s.case_id for s in scores if not s.correct),
    )


# ----------------------------------------------------------------------
# Internal parsing helpers
# ----------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be a mapping")
    return data


def _parse_header(raw: dict[str, Any], *, suite_name: str) -> SuiteHeader:
    if raw.get("suite") != suite_name:
        raise ValueError(
            f"suite header mismatch: expected {suite_name!r}, got {raw.get('suite')!r}"
        )
    version = int(raw.get("version", 0))
    if version != 1:
        raise ValueError(f"{suite_name}: unsupported version {version} (only v1 today)")
    threshold = float(raw.get("threshold", 0.0))
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"{suite_name}: threshold must be in [0,1], got {threshold}")
    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{suite_name}: cases must be a non-empty list")
    return SuiteHeader(version=version, suite=suite_name, threshold=threshold)


_VALID_INTENT_LABELS = {"simple", "medium", "hard", "handoff"}
_VALID_LANGUAGES = {"en", "ms", "zh-cn"}


def _parse_intent_case(data: Any, idx: int) -> IntentCase:
    if not isinstance(data, dict):
        raise ValueError(f"intent case #{idx} must be a mapping")
    case_id = str(data.get("id") or "").strip()
    if not case_id:
        raise ValueError(f"intent case #{idx} missing 'id'")
    text = str(data.get("input") or "").strip()
    if not text:
        raise ValueError(f"intent case {case_id!r}: 'input' is required + non-empty")
    expected = str(data.get("expected") or "").strip()
    if expected not in _VALID_INTENT_LABELS:
        raise ValueError(
            f"intent case {case_id!r}: expected must be one of "
            f"{sorted(_VALID_INTENT_LABELS)}, got {expected!r}"
        )
    language = str(data.get("language") or "").strip()
    if language not in _VALID_LANGUAGES:
        raise ValueError(
            f"intent case {case_id!r}: language must be one of "
            f"{sorted(_VALID_LANGUAGES)}, got {language!r}"
        )
    return IntentCase(
        id=case_id, input=text,
        expected=expected,        # type: ignore[arg-type]
        language=language,        # type: ignore[arg-type]
    )


def _parse_rag_case(data: Any, idx: int) -> RagCase:
    if not isinstance(data, dict):
        raise ValueError(f"rag case #{idx} must be a mapping")
    case_id = str(data.get("id") or "").strip()
    if not case_id:
        raise ValueError(f"rag case #{idx} missing 'id'")
    query = str(data.get("query") or "").strip()
    if not query:
        raise ValueError(f"rag case {case_id!r}: 'query' is required + non-empty")
    raw_chunks = data.get("seeded_chunks")
    if not isinstance(raw_chunks, list) or not raw_chunks:
        raise ValueError(
            f"rag case {case_id!r}: seeded_chunks must be a non-empty list"
        )
    chunks = tuple(_parse_seeded_chunk(c, case_id, i) for i, c in enumerate(raw_chunks))
    expected = str(data.get("expected_top_chunk") or "").strip()
    if not expected:
        raise ValueError(f"rag case {case_id!r}: expected_top_chunk required")
    chunk_ids = {c.id for c in chunks}
    if expected not in chunk_ids:
        raise ValueError(
            f"rag case {case_id!r}: expected_top_chunk {expected!r} "
            f"is not in seeded_chunks {sorted(chunk_ids)} — "
            f"unsatisfiable case"
        )
    return RagCase(
        id=case_id, query=query,
        seeded_chunks=chunks, expected_top_chunk=expected,
    )


def _parse_seeded_chunk(data: Any, case_id: str, idx: int) -> SeededChunk:
    if not isinstance(data, dict):
        raise ValueError(f"rag case {case_id!r} chunk #{idx} must be a mapping")
    cid = str(data.get("id") or "").strip()
    content = str(data.get("content") or "").strip()
    source = str(data.get("source") or "").strip()
    if not cid or not content or not source:
        raise ValueError(
            f"rag case {case_id!r} chunk #{idx}: id, content, source required"
        )
    return SeededChunk(id=cid, content=content, source=source)
