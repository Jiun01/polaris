"""Audience-member upsert on first inbound — Phase 14b gap.

When a genuinely new customer sends their first message, the pipeline
calls :func:`upsert_audience_member` so the contact appears in the
audience screen without requiring a CSV import or API sync. Contacts
acquired this way carry ``opt_in_source='inbound'`` (migration 078).

Design notes
------------
* Keyed on the partial-UNIQUE active membership index
  ``uniq_audience_active (tenant_id, channel, channel_user_id)
  WHERE opted_out_at IS NULL`` (migration 009). The ``ON CONFLICT``
  target therefore repeats that index predicate.

* On conflict we only fill ``channel_user_name`` / ``phone_number_e164``
  when they are currently NULL (``COALESCE(existing, incoming)``). We
  never touch ``opt_in_source`` or ``metadata`` — a contact first seen
  via CSV/api_sync/demo keeps that provenance and any metadata
  (e.g. ``intent='subscription'``) even after they message in.

* Caller scopes RLS via the ``app.current_tenant_id`` GUC already set
  for the inbound transaction; this function issues no ``SET LOCAL``.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

import asyncpg

logger = logging.getLogger(__name__)


async def upsert_audience_member(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    channel: str,
    channel_user_id: str,
    channel_user_name: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Create the active audience_members row for a contact if absent.

    Idempotent: a second inbound from the same contact only backfills a
    missing name/phone — it never overwrites ``opt_in_source`` or
    ``metadata``. ``phone_number_e164`` is derived for WhatsApp, where
    ``channel_user_id`` IS the E.164 number.
    """
    phone_e164 = channel_user_id if channel == "whatsapp" else None
    await conn.execute(
        """
        INSERT INTO audience_members
            (id, tenant_id, channel, channel_user_id, channel_user_name,
             phone_number_e164, opted_in_at, opt_in_source, metadata,
             created_at, updated_at)
        VALUES
            ($1, $2, $3, $4, $5, $6, now(), 'inbound', $7::jsonb, now(), now())
        ON CONFLICT (tenant_id, channel, channel_user_id)
            WHERE opted_out_at IS NULL
        DO UPDATE SET
            channel_user_name = COALESCE(
                audience_members.channel_user_name, EXCLUDED.channel_user_name
            ),
            phone_number_e164 = COALESCE(
                audience_members.phone_number_e164, EXCLUDED.phone_number_e164
            ),
            updated_at = now()
        """,
        uuid4(),
        tenant_id,
        channel,
        channel_user_id,
        channel_user_name,
        phone_e164,
        # Pass the dict DIRECTLY — pools.py registers a JSONB codec
        # (encoder=json.dumps) so asyncpg serialises dict→JSON object ONCE. A
        # ``json.dumps(...)`` string here would be double-encoded (the codec
        # re-dumps it) and stored as a JSONB *string* `"{}"` instead of an
        # object, breaking ``metadata->>'intent'`` filters. Same convention as
        # services/audit.py. Verified against Postgres.
        metadata or {},
    )
