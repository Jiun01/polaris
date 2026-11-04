"""Audience filter → SQL WHERE compiler — Phase 6 PR-5.

Pure function. No I/O. Takes a validated ``AudienceFilter`` Pydantic
model (from polaris_shared_types) and returns a tuple
``(where_sql, params)`` suitable for parameterised execution under
asyncpg.

NEVER string-concatenates values into the SQL. Every audience-side
value flows through the params list as $N placeholders. The dynamic
part is the CLAUSE TEMPLATE (which kind of comparison), driven by
the discriminator on filter_kind — that's a fixed allowlist, not
user input.

Compilation rules (per 04g §5.4):
    all_opted_in   -> TRUE                       (outer WHERE adds
                                                   opted_out_at IS NULL)
    tags_any       -> am.tags && $N::text[]
    tags_all       -> am.tags @> $N::text[]
    branch         -> am.tenant_branch_id = $N::uuid
    locale         -> am.locale = ANY($N::text[])
    channel        -> am.channel = ANY($N::text[])
    combined_and   -> recursive AND-join of parts; depth=1 only at GA.

The depth=1 invariant is enforced primarily by the Pydantic schema
in polaris_shared_types.broadcast (FilterCombinedAnd.parts excludes
FilterCombinedAnd from its Union). This module keeps a runtime
double-check so a future schema-widening doesn't silently change the
SQL surface — depth>=2 raises ``AudienceFilterDepthError``.

Use site:
    sql, params = compile_audience_filter(filt, starting_param_idx=2)
    # Caller composes the full INSERT...SELECT and passes
    # ($1=broadcast_id, $2=tenant_id, *params) — sql refers to
    # those placeholder positions starting at starting_param_idx.

The starting_param_idx parameter lets the caller place this WHERE
clause inside a larger query that already uses $1, $2, etc.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from polaris_shared_types import (
    AudienceFilter,
    FilterAllOptedIn,
    FilterBranch,
    FilterChannel,
    FilterCombinedAnd,
    FilterLocale,
    FilterTagsAll,
    FilterTagsAny,
)


class AudienceFilterDepthError(ValueError):
    """combined_and depth exceeded GA limit (depth=1 only).

    Pydantic should have rejected this at parse time; this guard
    fires if a future schema change widens the union without
    updating the SQL compiler to support CTE-rewrite for depth>=2.
    """


def compile_audience_filter(
    f: AudienceFilter, *, starting_param_idx: int = 1
) -> tuple[str, list[Any]]:
    """Compile a filter into (where_sql, params).

    ``starting_param_idx`` is the $N to use for the FIRST appended
    parameter — caller may have $1, $2 reserved for broadcast_id and
    tenant_id in the outer INSERT.
    """
    params: list[Any] = []
    sql = _compile(f, params, starting_param_idx, depth=0)
    return sql, params


def _compile(
    f: AudienceFilter,
    params: list[Any],
    base_idx: int,
    *,
    depth: int,
) -> str:
    """Recursive compile. ``params`` is appended in-place; the
    returned SQL string references $N placeholders starting at
    ``base_idx + len(params)`` BEFORE this call's appends.

    The CHECKpoint on depth=1 is "depth==0 OR we're inside a
    combined_and call which set depth=1". A combined_and inside a
    combined_and would hit depth==2 here.
    """
    if isinstance(f, FilterAllOptedIn):
        # The outer query owns "opted_out_at IS NULL"; this branch
        # contributes no extra restriction.
        return "TRUE"

    if isinstance(f, FilterTagsAny):
        params.append(list(f.tags))
        idx = base_idx + len(params) - 1
        return f"am.tags && ${idx}::text[]"

    if isinstance(f, FilterTagsAll):
        params.append(list(f.tags))
        idx = base_idx + len(params) - 1
        return f"am.tags @> ${idx}::text[]"

    if isinstance(f, FilterBranch):
        params.append(f.tenant_branch_id)
        idx = base_idx + len(params) - 1
        return f"am.tenant_branch_id = ${idx}::uuid"

    if isinstance(f, FilterLocale):
        params.append(list(f.locales))
        idx = base_idx + len(params) - 1
        return f"am.locale = ANY(${idx}::text[])"

    if isinstance(f, FilterChannel):
        params.append(list(f.channels))
        idx = base_idx + len(params) - 1
        return f"am.channel = ANY(${idx}::text[])"

    if isinstance(f, FilterCombinedAnd):
        if depth >= 1:
            raise AudienceFilterDepthError(
                "combined_and depth>=2 not supported at GA; CTE rewrite "
                "deferred"
            )
        parts_sql: list[str] = []
        for part in f.parts:
            parts_sql.append(_compile(part, params, base_idx, depth=depth + 1))
        # Wrap in parens so the AND-join binds correctly inside the
        # outer WHERE.
        return "(" + " AND ".join(parts_sql) + ")"

    # Unreachable: the discriminated union covers all variants. Defensive
    # guard so a future variant added to the Union but missed in this
    # compiler raises at runtime instead of returning an empty WHERE
    # (which would over-broadly match the whole audience — compliance
    # hazard).
    raise TypeError(
        f"compile_audience_filter: unknown filter kind {type(f).__name__}"
    )


def _unused_uuid_anchor() -> UUID:
    """Pin the UUID import so mypy + ruff don't complain about an
    unused import in modules that re-export this compiler. (Internal —
    the public surface only uses UUID via FilterBranch.tenant_branch_id.)
    """
    return UUID(int=0)
