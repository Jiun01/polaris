"""Shared error-envelope helper — extracted per Batch E (task E5; see PRP
plan audit-batch-e-structural-splits).

Mirrors the ``detail={"code": ..., "message"?: ..., **extra}`` convention
already used by ``routers/tenant_broadcasts.py``, ``tenant_audience.py``, and
``tenant_branches.py`` — this module does not invent a new shape, it just
gives that existing convention a one-line constructor so new call sites don't
have to hand-roll the dict.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def api_error(code: str, status: int, **extra: Any) -> HTTPException:
    """Build an ``HTTPException`` with the ``{"code", "message"?, ...extra}``
    envelope convention used by ``tenant_broadcasts.py`` / ``tenant_audience.py``
    / ``tenant_branches.py``.

    ``extra`` keyword arguments (e.g. ``message=...``, ``hint=...``) are
    merged into the envelope dict alongside ``code``.
    """
    return HTTPException(status_code=status, detail={"code": code, **extra})
