"""Meta Graph API mock router (E2E only).

Mounted at /meta-mock when POLARIS_META_MOCK_ENABLED=true.
The polaris-api itself calls this router when META_API_BASE_URL is
set to http://localhost:8000/meta-mock (local) or
http://polaris-api:8000/meta-mock (Docker).

All POST requests to /{version}/{path} are captured in the module-level
store and return a synthetic success response that mirrors the real
Graph API shape. GET /captured and DELETE /captured are NOT exposed here
(they live on /internal/e2e/meta-outbound-query and -clear to share
the E2E auth guard).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from polaris_api.services.meta_mock_store import get_all, record

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/{full_path:path}")
async def capture_meta_call(full_path: str, request: Request) -> JSONResponse:
    body: Any = {}
    try:
        body = await request.json()
    except Exception:
        raw = await request.body()
        body = raw.decode(errors="replace")

    await record(f"/{full_path}", body)
    captures = await get_all()
    logger.debug("meta-mock: captured POST /%s (total=%d)", full_path, len(captures))

    # Mirror the shape Meta returns for /messages — enough for callers to log
    # the provider_message_id without crashing. No synthetic rate-limit header:
    # the System Health Meta tile only ever reflects REAL Meta usage headers, so
    # it stays empty locally rather than showing fabricated headroom.
    return JSONResponse(
        content={"messages": [{"id": f"wamid.E2EMock{len(captures):04d}"}]},
        status_code=200,
    )
