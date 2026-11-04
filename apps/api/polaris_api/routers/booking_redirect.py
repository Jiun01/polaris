"""Public booking short-link redirect — ``GET /b/{code}`` (v2/phase-13).

Resolves the short booking link ``/b/{jti}`` that the bot sends customers
instead of the full signed EA wizard URL. The ``jti`` is the 32-hex
``binding_token`` stored in ``bookings``.

Security model
--------------
The route is unauthenticated — the customer has no Polaris session. Security
comes from the unguessable 32-hex ``jti`` (128 bits of entropy, same
capability model as the long signed URL it replaces). The redirect:

  1. Looks up the pending booking by ``jti`` in the service pool (BYPASSRLS).
  2. Returns a friendly 410 when the row is missing, expired, or status !=
     'pending', or when ``ea_service_id`` / ``ea_provider_id`` are NULL
     (pre-migration rows that can't be redirected deterministically).
  3. Re-mints the binding token with the REMAINING TTL so the short link
     can never extend the original 1-hour window.
  4. 307-redirects to the public EA wizard URL with ``polaris_ref``,
     ``service_id``, ``provider_id``, and (for WhatsApp) ``prefill_phone``.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from polaris_api.config import get_settings
from polaris_api.db.pools import get_service_pool
from polaris_api.services.booking.binding_token import (
    BindingTokenError,
)
from polaris_api.services.booking.binding_token import (
    mint as mint_binding_token,
)
from polaris_api.services.booking.client import build_ea_wizard_url

logger = logging.getLogger(__name__)

router = APIRouter(tags=["booking-redirect"])

_EXPIRED_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Link expired</title></head>
<body style="font-family:sans-serif;text-align:center;padding:3rem;">
<h2>This booking link has expired.</h2>
<p>Message us again to get a fresh one.</p>
</body>
</html>
"""


def _gone() -> HTMLResponse:
    """410 for an expired / unknown / un-redirectable short link."""
    return HTMLResponse(content=_EXPIRED_HTML, status_code=410)


@router.get("/b/{code}", include_in_schema=False, response_model=None)
async def booking_redirect(code: str) -> RedirectResponse | HTMLResponse:
    """Resolve a short booking link and redirect to the EA wizard.

    ``code`` is the 32-hex ``jti`` (``bookings.binding_token``). Returns 307
    on success, 410 when the link is expired / unknown / un-redirectable.
    """
    settings = get_settings()
    pool = get_service_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT b.id          AS booking_id,
                   b.tenant_id,
                   b.ea_service_id,
                   b.ea_provider_id,
                   b.channel,
                   b.customer_handle,
                   b.binding_expires_at,
                   t.slug        AS tenant_slug
              FROM bookings b
              JOIN tenants  t ON t.id = b.tenant_id
             WHERE b.binding_token      = $1
               AND b.status             = 'pending'
               AND b.binding_expires_at > now()
             LIMIT 1
            """,
            code,
        )

    if row is None:
        logger.info("booking_redirect: unknown/expired jti (first 8 chars: %s)", code[:8])
        return _gone()

    ea_service_id = row["ea_service_id"]
    ea_provider_id = row["ea_provider_id"]
    if ea_service_id is None or ea_provider_id is None:
        # Pre-migration-117 row — can't reconstruct wizard URL deterministically.
        logger.info(
            "booking_redirect: null ea ids — cannot redirect jti=%s", code[:8]
        )
        return _gone()

    tenant_slug: str = row["tenant_slug"]
    binding_expires_at: datetime = row["binding_expires_at"]
    channel: str = row["channel"]
    customer_handle: str = row["customer_handle"]
    booking_id = row["booking_id"]
    tenant_id = row["tenant_id"]

    # Re-mint with remaining TTL so the short link can never extend the window.
    now = datetime.now(UTC)
    # binding_expires_at may be naive (UTC) or tz-aware depending on DB driver.
    if binding_expires_at.tzinfo is None:
        binding_expires_at = binding_expires_at.replace(tzinfo=UTC)
    remaining_seconds = math.ceil((binding_expires_at - now).total_seconds())
    if remaining_seconds <= 0:
        # Race: expired between our SELECT and now.
        return _gone()

    try:
        token = mint_binding_token(
            jti=code,
            tenant_id=tenant_id,
            booking_id=booking_id,
            ttl_seconds=remaining_seconds,
        )
    except BindingTokenError:
        logger.warning(
            "booking_redirect: binding token mint failed jti=%s", code[:8]
        )
        return _gone()

    try:
        redirect_url = build_ea_wizard_url(
            base_url=settings.ea_booking_public_url,
            tenant_slug=tenant_slug,
            service_id=ea_service_id,
            provider_id=ea_provider_id,
            polaris_ref=token,
            channel=channel,
            customer_handle=customer_handle,
        )
    except ValueError:
        logger.warning(
            "booking_redirect: invalid slug %r jti=%s", tenant_slug, code[:8]
        )
        return _gone()

    return RedirectResponse(url=redirect_url, status_code=307)
