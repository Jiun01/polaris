"""CRUD against ``tenant_templates`` — Phase 6 PR-3.

Every function accepts an already-open ``asyncpg.Connection`` inside a
transaction that has ``app.current_tenant_id`` GUC set (the standard
``TenantContext.get_request_conn`` shape from Phase 1). RLS does the
tenant-isolation enforcement; we don't need a ``tenant_id`` WHERE on
these queries.

The lifecycle handler in ``lifecycle.py`` uses the BYPASSRLS service
pool because the inbound webhook arrives without a JWT — it sets the
GUC explicitly before calling ``update_status``.

Status transitions allowed:
  pending  → approved | rejected | paused | disabled
  approved → paused | rejected | disabled
  rejected → pending (resubmit on next portal POST)
  paused   → approved | rejected | disabled
  disabled → (terminal)

We don't enforce the transition graph in app code — Meta is the
source of truth and may flip status in any sequence (e.g. a
previously-approved template can be paused and later re-approved).
The DB CHECK only constrains the set of values, not the transitions.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg


async def list_templates(
    conn: asyncpg.Connection, *, status: str | None = None
) -> list[asyncpg.Record]:
    """List templates for the current tenant (RLS-scoped).

    Optional ``status`` filter ("approved" is the common portal use
    case — selecting an approved template for a broadcast). When
    None, returns every template ordered by submission time.
    """
    if status is None:
        rows = await conn.fetch(
            """
            SELECT id, tenant_id, tenant_channel_id, name, language,
                   category, status, meta_template_id, components,
                   rejection_reason, last_status_change_at,
                   submitted_at, created_at, updated_at
              FROM tenant_templates
             ORDER BY submitted_at DESC
            """
        )
        return list(rows)
    rows = await conn.fetch(
        """
        SELECT id, tenant_id, tenant_channel_id, name, language,
               category, status, meta_template_id, components,
               rejection_reason, last_status_change_at,
               submitted_at, created_at, updated_at
          FROM tenant_templates
         WHERE status = $1
         ORDER BY submitted_at DESC
        """,
        status,
    )
    return list(rows)


async def get_template(
    conn: asyncpg.Connection, template_id: UUID
) -> asyncpg.Record | None:
    """Return a single template by id (RLS-scoped). None if not found."""
    return await conn.fetchrow(
        """
        SELECT id, tenant_id, tenant_channel_id, name, language,
               category, status, meta_template_id, components,
               rejection_reason, last_status_change_at,
               submitted_at, created_at, updated_at
          FROM tenant_templates
         WHERE id = $1
        """,
        template_id,
    )


async def get_template_by_name(
    conn: asyncpg.Connection,
    *,
    tenant_channel_id: UUID,
    name: str,
    language: str,
) -> asyncpg.Record | None:
    """Look up a template by Meta's natural key.

    Meta's uniqueness is (waba_id, name, language). Our equivalent is
    (tenant_channel_id, name, language) because tenant_channel_id is
    the FK that resolves to waba_id. Used by the submit recovery path
    when Meta returned 409 (duplicate name) but our local row exists
    in 'pending' with meta_template_id IS NULL — we need to recover
    the id from a follow-up GET to Meta + persist.
    """
    return await conn.fetchrow(
        """
        SELECT id, tenant_id, tenant_channel_id, name, language,
               category, status, meta_template_id, components,
               rejection_reason, last_status_change_at,
               submitted_at, created_at, updated_at
          FROM tenant_templates
         WHERE tenant_channel_id = $1 AND name = $2 AND language = $3
        """,
        tenant_channel_id,
        name,
        language,
    )


async def get_template_by_meta_id(
    conn: asyncpg.Connection, meta_template_id: str
) -> asyncpg.Record | None:
    """Look up a template by Meta's assigned id.

    Used by the lifecycle webhook handler — it arrives with
    ``value.message_template_id`` and needs the tenant_id behind it.
    Caller is using the service pool (BYPASSRLS) so this returns
    cross-tenant; the caller then sets the GUC before any further
    work.
    """
    return await conn.fetchrow(
        """
        SELECT id, tenant_id, tenant_channel_id, name, language,
               category, status, meta_template_id, components,
               rejection_reason, last_status_change_at,
               submitted_at, created_at, updated_at
          FROM tenant_templates
         WHERE meta_template_id = $1
        """,
        meta_template_id,
    )


async def create_template(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    tenant_channel_id: UUID,
    name: str,
    language: str,
    category: str,
    components: list[dict[str, Any]],
    meta_template_id: str | None,
) -> UUID:
    """Insert a pending template; return its row id.

    ``meta_template_id`` is typically populated from the Meta submit
    response immediately — caller passes it as a single transactional
    write. If the Meta call timed out and we don't have an id yet,
    pass None and rely on the recovery path (GET to Meta + UPDATE
    meta_template_id) before the next submit attempt.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO tenant_templates
            (tenant_id, tenant_channel_id, name, language, category,
             components, meta_template_id, status,
             last_status_change_at, submitted_at, created_at, updated_at)
        VALUES
            ($1, $2, $3, $4, $5, $6::jsonb, $7, 'pending',
             now(), now(), now(), now())
        RETURNING id
        """,
        tenant_id,
        tenant_channel_id,
        name,
        language,
        category,
        # list DIRECTLY — JSONB codec serialises once (services/audit.py).
        components,
        meta_template_id,
    )
    assert row is not None  # noqa: S101 — INSERT...RETURNING always returns
    new_id: UUID = row["id"]
    return new_id


async def update_status(
    conn: asyncpg.Connection,
    *,
    template_id: UUID,
    status: str,
    rejection_reason: str | None = None,
    meta_template_id: str | None = None,
) -> None:
    """UPDATE template status. Stamps last_status_change_at.

    ``meta_template_id`` is updated only when explicitly provided —
    the lifecycle webhook may carry one we didn't have locally
    (timed-out submit recovery path). Pass None to leave unchanged.
    """
    if meta_template_id is None:
        await conn.execute(
            """
            UPDATE tenant_templates
               SET status = $2,
                   rejection_reason = $3,
                   last_status_change_at = now(),
                   updated_at = now()
             WHERE id = $1
            """,
            template_id,
            status,
            rejection_reason,
        )
    else:
        await conn.execute(
            """
            UPDATE tenant_templates
               SET status = $2,
                   rejection_reason = $3,
                   meta_template_id = $4,
                   last_status_change_at = now(),
                   updated_at = now()
             WHERE id = $1
            """,
            template_id,
            status,
            rejection_reason,
            meta_template_id,
        )


async def set_meta_template_id(
    conn: asyncpg.Connection,
    *,
    template_id: UUID,
    meta_template_id: str,
) -> None:
    """Late-bind meta_template_id without changing status.

    Used by the timed-out-submit recovery: we discovered an existing
    Meta template under the same name via GET, so we patch the id
    onto our pending row.
    """
    await conn.execute(
        """
        UPDATE tenant_templates
           SET meta_template_id = $2, updated_at = now()
         WHERE id = $1
        """,
        template_id,
        meta_template_id,
    )
