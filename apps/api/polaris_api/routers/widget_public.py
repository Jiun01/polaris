"""Public website-widget endpoint — off-Meta channels M3.

Mounted at ``/api/v1/widget`` (PUBLIC — no auth; keyed by the opaque widget_key).

  * ``GET /{key}/config`` — the bubble fetches this cross-origin to render itself:
    greeting + the list of channels, each with its **direct https target**
    (t.me / wa.me / ...). The bubble links each channel straight at its target,
    so there is NO server redirect: a direct ``<a href>`` tap is lighter (no
    public write endpoint) and more reliable on mobile (a 302 breaks the iOS
    Universal-Link user-gesture chain; a direct anchor opens the native app).

Targets are safe to expose + render as anchor hrefs because they are validated
against the chat-host allowlist (https only, t.me/wa.me/m.me/ig.me) at WRITE time
(see tenant_widget router). Disabled or unknown widgets 404 (revealing nothing).
The response carries permissive CORS (fetched from arbitrary tenant origins,
non-sensitive) and a short cache, applied only on success so a transient/disabled
404 is never cached.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Response, status
from pydantic import BaseModel, ConfigDict

from polaris_api.services import widget as widget_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["widget-public"])

_FORBID = ConfigDict(extra="forbid")


class _PublicChannel(BaseModel):
    model_config = _FORBID
    kind: str
    label: str
    target: str


class _PublicConfigResponse(BaseModel):
    model_config = _FORBID
    greeting: str
    channels: list[_PublicChannel]


@router.get("/{widget_key}/config", response_model=_PublicConfigResponse)
async def get_widget_config(
    widget_key: Annotated[str, Path(min_length=1, max_length=64)],
    response: Response,
) -> _PublicConfigResponse:
    """Public config for the bubble. 404 when unknown/disabled."""
    # Permissive CORS — public, non-sensitive, fetched from tenant origins.
    response.headers["Access-Control-Allow-Origin"] = "*"

    config = await widget_service.get_public_config(widget_key)
    if config is None:
        # Raise (not a mutated 200 body) AND do NOT set Cache-Control, so a CDN
        # can't cache a transient/disabled 404 for the cache window.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    response.headers["Cache-Control"] = "public, max-age=60"

    channels: list[_PublicChannel] = []
    for entry in config.channels:
        if not isinstance(entry, dict):
            continue
        target = str(entry.get("target", ""))
        # Defence-in-depth: only emit allowlisted https targets. Write-time
        # validation already guarantees this; this is a second gate so a
        # malformed row can never reach the client as an anchor href.
        if not widget_service.is_allowed_target(target):
            continue
        channels.append(
            _PublicChannel(
                kind=str(entry.get("kind", "")),
                label=str(entry.get("label", "")),
                target=target,
            )
        )
    return _PublicConfigResponse(greeting=config.greeting, channels=channels)
