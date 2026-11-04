"""Shared ``{{varname}}`` substitution.

Used by ``chatwoot/handoff.py`` to render customer-facing handoff messages
and private notes. Kept as one implementation so callers can't drift.
``{{varname}}`` refs are replaced with ``str(value)``; missing / ``None``
values become empty strings.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

_VAR_REF_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def substitute(template: str, variables: Mapping[str, object]) -> str:
    """Replace ``{{varname}}`` references with ``str(variables[varname])``.

    Missing variables (absent or ``None``) become empty strings.
    """

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = variables.get(name)
        return "" if value is None else str(value)

    return _VAR_REF_RE.sub(_replace, template)
