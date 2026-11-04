"""Branch-centric EA routing lookup (v2.1 / PR-1).

Resolves the EA provider/service a booking should target based on the
branch that owns the conversation's inbox. The inbound pipeline stamps
``conversations.tenant_branch_id`` from the resolved inbox
(``tenant_channels.tenant_branch_id``) at conversation creation; this
helper reads the branch's first-class EA columns.

Returns ``(ea_provider_id, ea_default_service_id)`` — either may be
``None`` (no branch on the conversation, or the branch has no EA config).
The booking node treats ``None`` as "fall back to the flow-hardcoded
ids", preserving GA behaviour for single-branch tenants.

Uses the BYPASSRLS service pool because the booking node runs inside the
inbound pipeline where the tenant is already trusted (resolved from the
signed webhook); the explicit ``tenant_id`` filter scopes the read.
"""

from __future__ import annotations

import logging
from uuid import UUID

from polaris_api.db.pools import get_service_pool

logger = logging.getLogger(__name__)


async def get_branch_ea_config(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
) -> tuple[int | None, int | None]:
    """Return ``(ea_provider_id, ea_default_service_id)`` for the branch
    owning ``conversation_id``.

    ``(None, None)`` when the conversation has no branch or the branch
    has no EA routing configured.
    """
    pool = get_service_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT tb.ea_provider_id        AS ea_provider_id,
                   tb.ea_default_service_id  AS ea_default_service_id
              FROM conversations c
              JOIN tenant_branches tb ON tb.id = c.tenant_branch_id
             WHERE c.id = $1 AND c.tenant_id = $2
            """,
            conversation_id,
            tenant_id,
        )
    if row is None:
        # No branch on the conversation (or conversation gone) — caller
        # falls back to flow-hardcoded ids.
        return None, None
    return row["ea_provider_id"], row["ea_default_service_id"]
