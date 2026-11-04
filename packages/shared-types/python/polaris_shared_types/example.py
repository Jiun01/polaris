"""Example shared type — proves the Pydantic -> JSON Schema -> TS pipeline.

Phase 1 will replace `example.py` with the real cross-runtime shapes (Tenant,
Channel, Flow, Message, etc.). For Phase 0 this is the seed.

`Literal` instead of `str + Field(pattern=...)`:
- Strict mypy + pyright narrow to the literal union (catches typos).
- Pydantic v2 emits `enum` in JSON Schema instead of a `string` + regex,
  so json-schema-to-typescript generates a TS string-union type rather
  than `string`. Downstream consumers get type-checked at the call site.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthStatus(BaseModel):
    """Single source of truth for the GET /health response shape."""

    status: Literal["ok", "degraded", "down"]
    supabase: Literal["reachable", "unreachable"]
    redis: Literal["reachable", "unreachable"]
    version: str
