"""Audience-filter SQL compiler tests — Phase 6 PR-5.

Pure-function coverage of every filter_kind variant. The compiler is
deterministic + I/O-free so we can run a full corpus locally.

Invariants:
  * The returned SQL refers only to ``am.<column>`` qualifiers + ``$N``
    placeholders. No value ever appears literally in the SQL.
  * ``params`` carries the exact runtime values + asyncpg coerces them
    via the explicit ``::text[]`` / ``::uuid`` casts.
  * starting_param_idx shifts every $N reference by the right offset.
  * combined_and depth>=2 raises AudienceFilterDepthError at the SQL
    layer (Pydantic also rejects it at schema-parse time).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from polaris_api.services.audience.filter import (
    AudienceFilterDepthError,
    compile_audience_filter,
)
from polaris_shared_types import (
    FilterAllOptedIn,
    FilterBranch,
    FilterChannel,
    FilterCombinedAnd,
    FilterLocale,
    FilterTagsAll,
    FilterTagsAny,
)


def test_all_opted_in_compiles_to_TRUE() -> None:
    sql, params = compile_audience_filter(
        FilterAllOptedIn(filter_kind="all_opted_in")
    )
    assert sql == "TRUE"
    assert params == []


def test_tags_any_uses_overlap_operator() -> None:
    sql, params = compile_audience_filter(
        FilterTagsAny(filter_kind="tags_any", tags=["vip", "loyalty"]),
        starting_param_idx=1,
    )
    assert sql == "am.tags && $1::text[]"
    assert params == [["vip", "loyalty"]]


def test_tags_all_uses_contains_operator() -> None:
    sql, params = compile_audience_filter(
        FilterTagsAll(filter_kind="tags_all", tags=["en", "kl"]),
        starting_param_idx=5,
    )
    assert sql == "am.tags @> $5::text[]"
    assert params == [["en", "kl"]]


def test_branch_uses_uuid_cast() -> None:
    branch_id = uuid4()
    sql, params = compile_audience_filter(
        FilterBranch(filter_kind="branch", tenant_branch_id=branch_id),
        starting_param_idx=2,
    )
    assert sql == "am.tenant_branch_id = $2::uuid"
    assert params == [branch_id]


def test_locale_uses_any_array() -> None:
    sql, params = compile_audience_filter(
        FilterLocale(filter_kind="locale", locales=["ms-MY", "en-MY"]),
        starting_param_idx=3,
    )
    assert sql == "am.locale = ANY($3::text[])"
    assert params == [["ms-MY", "en-MY"]]


def test_channel_uses_any_array() -> None:
    sql, params = compile_audience_filter(
        FilterChannel(filter_kind="channel", channels=["whatsapp"]),
        starting_param_idx=1,
    )
    assert sql == "am.channel = ANY($1::text[])"
    assert params == [["whatsapp"]]


def test_combined_and_joins_with_AND_and_parens() -> None:
    sql, params = compile_audience_filter(
        FilterCombinedAnd(
            filter_kind="combined_and",
            parts=[
                FilterTagsAny(filter_kind="tags_any", tags=["vip"]),
                FilterLocale(filter_kind="locale", locales=["ms-MY"]),
            ],
        ),
        starting_param_idx=3,
    )
    # Outer parens + AND-join.
    assert sql == "(am.tags && $3::text[] AND am.locale = ANY($4::text[]))"
    assert params == [["vip"], ["ms-MY"]]


def test_combined_and_param_indices_increment_correctly() -> None:
    """Param indices must increase monotonically as we walk the AND
    parts — off-by-one here would silently send the wrong value to
    each clause."""
    sql, params = compile_audience_filter(
        FilterCombinedAnd(
            filter_kind="combined_and",
            parts=[
                FilterChannel(filter_kind="channel", channels=["whatsapp"]),
                FilterTagsAll(filter_kind="tags_all", tags=["loyalty"]),
                FilterTagsAny(filter_kind="tags_any", tags=["vip"]),
            ],
        ),
        starting_param_idx=10,
    )
    assert sql == (
        "(am.channel = ANY($10::text[]) AND am.tags @> $11::text[] "
        "AND am.tags && $12::text[])"
    )
    assert params == [["whatsapp"], ["loyalty"], ["vip"]]


def test_combined_and_depth_2_raises_at_runtime() -> None:
    """Pydantic schema excludes FilterCombinedAnd from parts[], so
    schema-parse rejects depth=2 first. The compiler's runtime guard
    is defence-in-depth — if a future Union widen slips through, we
    surface a clear error rather than producing broken SQL.

    We test this by bypassing Pydantic construction (using
    object.__setattr__ on a depth-1 instance) since the model_validator
    on FilterCombinedAnd is also unreachable in the normal Pydantic
    path."""
    inner = FilterCombinedAnd(
        filter_kind="combined_and",
        parts=[
            FilterTagsAny(filter_kind="tags_any", tags=["a"]),
            FilterTagsAny(filter_kind="tags_any", tags=["b"]),
        ],
    )
    outer = FilterCombinedAnd.model_construct(
        filter_kind="combined_and",
        parts=[
            inner,  # type: ignore[list-item]
            FilterTagsAny(filter_kind="tags_any", tags=["c"]),
        ],
    )

    with pytest.raises(AudienceFilterDepthError):
        compile_audience_filter(outer)


def test_param_placeholders_match_param_count() -> None:
    """For each composed filter, the highest $N in the SQL must equal
    starting_param_idx + len(params) - 1 (i.e. every param has a
    corresponding placeholder and vice versa)."""
    import re

    cases = [
        FilterAllOptedIn(filter_kind="all_opted_in"),
        FilterTagsAny(filter_kind="tags_any", tags=["a"]),
        FilterCombinedAnd(
            filter_kind="combined_and",
            parts=[
                FilterChannel(filter_kind="channel", channels=["whatsapp"]),
                FilterLocale(filter_kind="locale", locales=["ms-MY"]),
            ],
        ),
    ]
    for case in cases:
        sql, params = compile_audience_filter(case, starting_param_idx=7)
        placeholders = {int(m) for m in re.findall(r"\$(\d+)", sql)}
        if params:
            assert placeholders == set(range(7, 7 + len(params)))
        else:
            assert placeholders == set()
