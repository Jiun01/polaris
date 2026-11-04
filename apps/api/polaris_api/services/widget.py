"""Website launcher widget service — off-Meta channels M3/M4.

Owns the non-router logic for the click-to-chat bubble:
  - the chat-host allowlist + target validation (write-time safety),
  - public widget-key generation,
  - reading a tenant's config by public key (service pool, no tenant context).

The widget is a launcher only — it never hosts a conversation. ``target`` URLs
point at the tenant's real channels (t.me / wa.me / m.me / ig.me https deep
links). READ-ONLY design: the bubble links DIRECTLY at those targets (no server
redirect, no click write), so ``is_allowed_target`` is the WRITE-time gate that
guarantees every stored target is a safe https chat link the client can render
as an anchor href without an open-redirect / javascript: risk.
"""

from __future__ import annotations

import secrets as pysecrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from polaris_api.db.pools import get_service_pool

# Chat deep-link hosts the redirect is allowed to forward to. Anything else is
# rejected at config-write time AND at redirect time, so a compromised/edited
# config row can never turn the redirect into an open redirect.
_ALLOWED_HOSTS: frozenset[str] = frozenset({"t.me", "wa.me", "m.me", "ig.me"})


def is_allowed_target(url: str) -> bool:
    """True if ``url`` is https and its host is an allowlisted chat host.

    Rejects userinfo (``user:pass@host``) and explicit ports: ``urlparse`` reads
    ``hostname`` as the part AFTER ``@``, so ``https://evil.com@t.me/x`` would
    otherwise pass the host check yet redirect browsers to evil.com (open
    redirect). None of the four chat hosts ever need userinfo or a non-443 port.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if parsed.username or parsed.password or parsed.port is not None:
        return False
    host = (parsed.hostname or "").lower()
    return host in _ALLOWED_HOSTS


def gen_widget_key() -> str:
    """Opaque, non-secret public key embedded in the tenant's <script> tag.

    URL-safe + unguessable enough that the config endpoint isn't trivially
    enumerable, but it is NOT a credential (config is public by design).
    """
    return pysecrets.token_urlsafe(16)


@dataclass(frozen=True)
class WidgetConfigRow:
    tenant_id: UUID
    enabled: bool
    greeting: str
    channels: list[dict[str, Any]]


async def get_public_config(widget_key: str) -> WidgetConfigRow | None:
    """Read a tenant's widget config by public key via the service pool.

    Returns ``None`` when no row matches or the widget is disabled — the public
    endpoints treat both as 404 so a disabled widget reveals nothing.
    """
    pool = get_service_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT tenant_id, enabled, greeting, channels
              FROM tenant_widget_config
             WHERE widget_key = $1
            """,
            widget_key,
        )
    if row is None or not row["enabled"]:
        return None
    channels = row["channels"]
    if not isinstance(channels, list):
        channels = []
    return WidgetConfigRow(
        tenant_id=row["tenant_id"],
        enabled=row["enabled"],
        greeting=row["greeting"],
        channels=channels,
    )
