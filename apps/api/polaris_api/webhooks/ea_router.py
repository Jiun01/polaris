"""EA (Easy!Appointments fork) inbound webhook — v2.1 / PR-1.

Mounted at ``/v1/webhooks/ea``. The polaris-ea-fork fires this on
appointment create/update, echoing back the ``polaris_ref`` binding
token we embedded in the booking link. We verify the token + an HMAC
over the raw body, then flip the matching pending booking to confirmed.

HMAC posture mirrors the Meta/Chatwoot webhooks: the fork signs the raw
request body with a shared secret and sends
``X-Polaris-Signature: sha256=<hexdigest>``. An empty configured secret
fails closed (401) — no "empty signs empty" bypass.

Idempotent: EA may retry. ``confirm_booking`` reports ``already_confirmed``
on re-delivery and we still ack 200 so the fork stops retrying.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from polaris_api.config import get_settings
from polaris_api.services.booking import link_flow
from polaris_api.services.booking.binding_token import (
    BindingTokenError,
)
from polaris_api.services.booking.binding_token import (
    verify as verify_binding_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ea-webhook"])


class EAWebhookPayload(BaseModel):
    """Body the EA fork posts on appointment create/update."""

    polaris_ref: str = Field(min_length=1, max_length=2048)
    appointment_id: str = Field(min_length=1, max_length=128)
    start_datetime: str | None = Field(default=None, max_length=64)
    end_datetime: str | None = Field(default=None, max_length=64)
    service_name: str | None = Field(default=None, max_length=200)


def _verify_signature(raw_body: bytes, signature_header: str | None) -> None:
    """Constant-time HMAC-SHA256 check of the raw body. Fails closed."""
    secret = get_settings().ea_webhook_hmac_secret
    if not secret or secret.startswith("placeholder"):
        logger.warning("ea webhook called but ea_webhook_hmac_secret unset — 401")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="ea_webhook_unconfigured"
        )
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="bad_signature_format"
        )
    provided = signature_header.split("=", 1)[1]
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="signature_mismatch"
        )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # bookings.start_at is TIMESTAMPTZ; tag a naive EA datetime as UTC
    # explicitly rather than letting asyncpg infer the server's local zone.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@router.post("/ea", status_code=status.HTTP_200_OK)
async def ea_inbound(
    request: Request,
    x_polaris_signature: str | None = Header(default=None),
) -> dict[str, str]:
    """Confirm a pending booking from an EA appointment webhook."""
    raw = await request.body()
    _verify_signature(raw, x_polaris_signature)

    try:
        payload = EAWebhookPayload.model_validate_json(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="bad_payload"
        ) from exc

    try:
        binding = verify_binding_token(payload.polaris_ref)
    except BindingTokenError as exc:
        # A bad/expired token is not retryable — ack 200 so the fork
        # stops, but log loudly for forensics.
        logger.warning("ea webhook: binding token rejected: %s", type(exc).__name__)
        return {"outcome": "rejected_token"}

    start_at = _parse_dt(payload.start_datetime)
    if start_at is None:
        # A confirmed booking must carry a slot (DB CHECK enforces it).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="missing_or_invalid_start_datetime",
        )

    result = await link_flow.confirm_booking(
        tenant_id=binding.tenant_id,
        jti=binding.jti,
        appointment_id=payload.appointment_id,
        start_at=start_at,
        end_at=_parse_dt(payload.end_datetime),
        service_name=payload.service_name,
    )
    logger.info(
        "ea webhook: tenant_id=%s appointment_id=%s outcome=%s",
        binding.tenant_id,
        payload.appointment_id,
        result.outcome,
    )
    return {"outcome": result.outcome}
