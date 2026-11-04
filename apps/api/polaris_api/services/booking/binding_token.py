"""HMAC binding token for booking-via-EA-link (v2.1 / PR-1).

When the booking node sends a customer an EA web link, the booking made
on EA's page must be tied back to the VERIFIED chat identity (the
phone / IGSID / PSID from the signed Meta webhook), not to whatever the
customer types into EA's web form. We carry that binding across the
detour with a server-minted, HMAC-signed token embedded in the link as
``?polaris_ref=<token>``.

Token format (compact, URL-safe):

    v1.<b64url(payload_json)>.<b64url(hmac_sha256(secret, payload_json_b64))>

``payload`` = ``{"jti": <opaque id>, "tid": <tenant_id>,
"bid": <booking_id>, "exp": <unix_seconds>}``.

The ``jti`` is the lookup key stored in ``bookings.binding_token``; the
full signed token only ever lives in the URL. On the inbound EA webhook
we verify the signature + expiry, extract the ``jti``, and resolve the
pending booking by it.

Security properties:
  * The customer never types the binding — it is minted server-side and
    signed, so it cannot be forged or pointed at another booking.
  * ``hmac.compare_digest`` for constant-time signature comparison.
  * Fails closed: an empty / placeholder secret raises rather than
    minting an unverifiable token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from uuid import UUID

from polaris_api.config import get_settings

_TOKEN_VERSION = "v1"  # noqa: S105 — token format version tag, not a secret


class BindingTokenError(Exception):
    """Base for binding-token failures (verify-side)."""


class BindingTokenConfigError(BindingTokenError):
    """The signing secret is unset / placeholder — fail closed rather
    than mint or accept an unverifiable token."""


class BindingTokenInvalid(BindingTokenError):
    """Malformed, tampered, or expired token."""


def _resolve_secret(secret: str | None) -> bytes:
    """Return the signing key as bytes, or raise if unusable.

    ``secret`` override exists for unit tests; production reads
    ``settings.booking_binding_secret``.
    """
    value = secret if secret is not None else get_settings().booking_binding_secret
    if not value or value.startswith("placeholder"):
        raise BindingTokenConfigError(
            "booking_binding_secret is unset or a placeholder — refusing to "
            "mint/verify a binding token with an unusable key"
        )
    return value.encode("utf-8")


def ensure_configured(secret: str | None = None) -> None:
    """Raise ``BindingTokenConfigError`` if the signing secret is unusable.

    Lets a caller fail fast *before* taking side effects (e.g. writing a
    pending booking row) when the binding secret is misconfigured.
    """
    _resolve_secret(secret)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload_b64: str, key: bytes) -> str:
    digest = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def mint(
    *,
    jti: str,
    tenant_id: UUID,
    booking_id: UUID,
    ttl_seconds: int | None = None,
    secret: str | None = None,
    now: int | None = None,
) -> str:
    """Mint a signed binding token.

    ``ttl_seconds`` defaults to ``settings.booking_binding_ttl_seconds``.
    ``now`` override exists for deterministic tests.
    """
    key = _resolve_secret(secret)
    settings = get_settings()
    ttl = ttl_seconds if ttl_seconds is not None else settings.booking_binding_ttl_seconds
    issued = now if now is not None else int(time.time())
    payload = {
        "jti": jti,
        "tid": str(tenant_id),
        "bid": str(booking_id),
        "exp": issued + ttl,
    }
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    sig_b64 = _sign(payload_b64, key)
    return f"{_TOKEN_VERSION}.{payload_b64}.{sig_b64}"


class VerifiedBinding:
    """Result of a successful ``verify`` — the trusted token claims."""

    __slots__ = ("jti", "tenant_id", "booking_id", "exp")

    def __init__(self, *, jti: str, tenant_id: UUID, booking_id: UUID, exp: int) -> None:
        self.jti = jti
        self.tenant_id = tenant_id
        self.booking_id = booking_id
        self.exp = exp


def verify(
    token: str,
    *,
    secret: str | None = None,
    now: int | None = None,
) -> VerifiedBinding:
    """Verify a token's signature + expiry and return its claims.

    Raises ``BindingTokenInvalid`` on any malformation, signature
    mismatch, or expiry; ``BindingTokenConfigError`` if the secret is
    unusable.
    """
    key = _resolve_secret(secret)
    if not isinstance(token, str) or token.count(".") != 2:
        raise BindingTokenInvalid("token is not a well-formed v1 token")
    version, payload_b64, sig_b64 = token.split(".", 2)
    if version != _TOKEN_VERSION:
        raise BindingTokenInvalid(f"unsupported token version {version!r}")

    expected_sig = _sign(payload_b64, key)
    if not hmac.compare_digest(expected_sig, sig_b64):
        raise BindingTokenInvalid("signature mismatch")

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise BindingTokenInvalid("payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise BindingTokenInvalid("payload is not an object")

    try:
        jti = str(payload["jti"])
        tenant_id = UUID(str(payload["tid"]))
        booking_id = UUID(str(payload["bid"]))
        exp = int(payload["exp"])
    except (KeyError, ValueError) as exc:
        raise BindingTokenInvalid("payload missing/invalid claims") from exc

    current = now if now is not None else int(time.time())
    if current >= exp:
        raise BindingTokenInvalid("token expired")

    return VerifiedBinding(jti=jti, tenant_id=tenant_id, booking_id=booking_id, exp=exp)
