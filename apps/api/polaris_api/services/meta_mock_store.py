"""In-memory capture store for the Meta Graph API mock (E2E only).

Thread-safe via asyncio.Lock. All captured requests are stored in a
module-level list so the /meta-mock and /internal/e2e/meta-outbound-*
routers share the same state within one process lifetime.

Cleared between test cases via DELETE /internal/e2e/meta-outbound-clear.
"""

from __future__ import annotations

import asyncio
from typing import Any

_lock: asyncio.Lock = asyncio.Lock()
_captures: list[dict[str, Any]] = []


async def record(path: str, body: Any) -> None:
    async with _lock:
        _captures.append({"path": path, "body": body})


async def get_all() -> list[dict[str, Any]]:
    async with _lock:
        return list(_captures)


async def clear() -> int:
    async with _lock:
        n = len(_captures)
        _captures.clear()
        return n
