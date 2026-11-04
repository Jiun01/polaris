"""RAG eval scoring helpers — pure, DB-free, unit-tested.

The retrieval eval harness (``apps/api/scripts/rag_eval.py``) runs the REAL
tier-aware retrieval path (``services.rag.search_hybrid``) over a hand-curated
golden set and scores **recall@k** and **MRR** — turning "is the KB good?" from
eyeballing chunk previews into a number, broken down by language.

This module holds ONLY the scoring math so it can be unit-tested without a
database, an embedding provider, or a network call:

  * :func:`hit_rank` — for one question, the 1-based rank of the first retrieved
    chunk that satisfies the expectation (keyword match + optional page match),
    else ``None`` (a miss).
  * :func:`recall_at_k` / :func:`mrr` — the two standard cheap retrieval metrics
    over a list of per-question ranks.
  * :func:`summarize` — overall + per-language rollup plus the failure list, the
    shape the runner prints.

Matching is **keyword/substring**, lowercased, field-tolerant: a hit's chunk
text lives on ``KnowledgeChunkHit.content`` and its source label on
``.source_name`` (confirmed from ``polaris_shared_types.KnowledgeChunkHit``).
The helpers read those via ``getattr`` so a plain stand-in object (or a future
field rename) still scores — the unit tests use lightweight stubs, not the real
Pydantic model.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def _hit_text(hit: Any) -> str:
    """Lowercased chunk text for an opaque hit, '' if the field is absent.

    Reads ``.content`` (the real ``KnowledgeChunkHit`` text field). ``getattr``
    keeps the scorer tolerant of a stub or a future rename rather than raising
    ``AttributeError`` mid-eval.
    """
    return str(getattr(hit, "content", "") or "").lower()


def _hit_source(hit: Any) -> str:
    """Lowercased source label for an opaque hit, '' if the field is absent.

    Reads ``.source_name`` (the real citation/display field). Used only when a
    golden row supplies ``expect_page`` — page-match is an optional extra
    constraint ON TOP OF the keyword match, never a substitute for it.
    """
    return str(getattr(hit, "source_name", "") or "").lower()


def hit_rank(
    hits: Sequence[Any],
    expect_any: Sequence[str],
    expect_page: str | None = None,
) -> int | None:
    """1-based rank of the first hit that satisfies the expectation, else ``None``.

    A hit satisfies the expectation when its lowercased ``content`` contains ANY
    of the ``expect_any`` keywords (lowercased substring match) AND — when
    ``expect_page`` is given — its lowercased ``source_name`` contains the
    (lowercased) ``expect_page`` fragment. ``expect_page`` is optional: omit it
    for a keyword-only check.

    Returns the rank (``1`` = top hit) of the FIRST satisfying chunk, or
    ``None`` if no retrieved chunk satisfies it (a recall miss). An empty
    ``expect_any`` can never match → always ``None`` (a malformed golden row
    scores as a miss rather than a spurious pass).
    """
    keywords = [kw.lower() for kw in expect_any if kw]
    if not keywords:
        return None
    page = expect_page.lower() if expect_page else None
    for rank, hit in enumerate(hits, start=1):
        text = _hit_text(hit)
        if not any(kw in text for kw in keywords):
            continue
        if page is not None and page not in _hit_source(hit):
            continue
        return rank
    return None


def recall_at_k(ranks: Sequence[int | None], k: int) -> float:
    """Fraction of questions whose expected fact was retrieved within the top-``k``.

    A question counts as recalled when its rank is non-``None`` and ``<= k``.
    Returns ``0.0`` for an empty input (empty-safe — no ``ZeroDivisionError``).
    """
    if not ranks:
        return 0.0
    hits = sum(1 for r in ranks if r is not None and r <= k)
    return hits / len(ranks)


def mrr(ranks: Sequence[int | None]) -> float:
    """Mean Reciprocal Rank over the per-question ranks.

    Each question contributes ``1 / rank`` (so rank 1 → 1.0, rank 2 → 0.5, …)
    or ``0.0`` when it was a miss (``None``). Returns ``0.0`` for an empty input
    (empty-safe).
    """
    if not ranks:
        return 0.0
    total = sum((1.0 / r) for r in ranks if r is not None)
    return total / len(ranks)


def summarize(rows: Sequence[dict[str, Any]], k: int = 5) -> dict[str, Any]:
    """Roll per-question results up into the report shape the runner prints.

    Each input row is a scored golden question::

        {"q": str, "lang": str, "rank": int | None, "expect_any": list[str],
         "expect_page": str | None}

    Returns::

        {
          "count": int,
          "k": int,
          "recall_at_k": float,        # overall
          "mrr": float,                # overall
          "by_lang": {                 # one entry per language present
            "en": {"count": int, "recall_at_k": float, "mrr": float}, ...
          },
          "failures": [                # rows whose rank is None (a miss)
            {"q": str, "lang": str, "expect_any": [...], "expect_page": ...}, ...
          ],
        }

    Empty-safe: ``rows == []`` → zero counts, ``0.0`` metrics, empty ``by_lang``
    and ``failures`` (no division by zero).
    """
    ranks: list[int | None] = [row.get("rank") for row in rows]
    by_lang: dict[str, dict[str, Any]] = {}
    # Preserve first-seen language order for stable, readable report output.
    for row in rows:
        lang = str(row.get("lang") or "unknown")
        by_lang.setdefault(lang, {"_ranks": []})["_ranks"].append(row.get("rank"))

    by_lang_out: dict[str, dict[str, Any]] = {}
    for lang, bucket in by_lang.items():
        lang_ranks: list[int | None] = bucket["_ranks"]
        by_lang_out[lang] = {
            "count": len(lang_ranks),
            "recall_at_k": recall_at_k(lang_ranks, k),
            "mrr": mrr(lang_ranks),
        }

    failures = [
        {
            "q": row.get("q", ""),
            "lang": str(row.get("lang") or "unknown"),
            "expect_any": list(row.get("expect_any", [])),
            "expect_page": row.get("expect_page"),
        }
        for row in rows
        if row.get("rank") is None
    ]

    return {
        "count": len(rows),
        "k": k,
        "recall_at_k": recall_at_k(ranks, k),
        "mrr": mrr(ranks),
        "by_lang": by_lang_out,
        "failures": failures,
    }
