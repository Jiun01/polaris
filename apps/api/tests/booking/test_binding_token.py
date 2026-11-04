"""Pure-unit tests for services/booking/binding_token.py (v2.1 PR-1).

No DB, no network, no settings file required.  All crypto is in-process.
Every test passes ``secret=`` directly to ``mint``/``verify`` so the
lru-cached settings object is never consulted.

Coverage matrix:
  * mint → verify round-trip (happy path)
  * tampered payload → BindingTokenInvalid
  * tampered signature → BindingTokenInvalid
  * expired token (``now=`` past exp) → BindingTokenInvalid
  * zero-TTL (exp == now) → BindingTokenInvalid
  * empty secret → BindingTokenConfigError on mint
  * empty secret → BindingTokenConfigError on verify
  * placeholder secret → BindingTokenConfigError on mint
  * placeholder secret → BindingTokenConfigError on verify
  * token with wrong segment count → BindingTokenInvalid
  * token with wrong version prefix → BindingTokenInvalid
  * garbage base64 in payload segment → BindingTokenInvalid
  * payload that is not a JSON object → BindingTokenInvalid
  * payload missing required claims → BindingTokenInvalid
  * payload with non-UUID tid/bid → BindingTokenInvalid
"""

from __future__ import annotations

import base64
import json
from uuid import uuid4

import pytest
from polaris_api.services.booking.binding_token import (
    BindingTokenConfigError,
    BindingTokenInvalid,
    VerifiedBinding,
    mint,
    verify,
)

_SECRET = "test-binding-secret-not-placeholder"
_FIXED_NOW = 1_700_000_000  # arbitrary fixed epoch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _mint_token(
    *,
    jti: str | None = None,
    tenant_id=None,
    booking_id=None,
    ttl_seconds: int = 3600,
    now: int = _FIXED_NOW,
) -> str:
    return mint(
        jti=jti or "test-jti-1234",
        tenant_id=tenant_id or uuid4(),
        booking_id=booking_id or uuid4(),
        ttl_seconds=ttl_seconds,
        secret=_SECRET,
        now=now,
    )


# ---------------------------------------------------------------------------
# Happy path — round-trip
# ---------------------------------------------------------------------------


def test_mint_verify_round_trip_returns_correct_claims() -> None:
    """mint() then verify() must return the exact claims that were minted."""
    jti = "round-trip-jti"
    tenant_id = uuid4()
    booking_id = uuid4()
    now = _FIXED_NOW

    token = mint(
        jti=jti,
        tenant_id=tenant_id,
        booking_id=booking_id,
        ttl_seconds=3600,
        secret=_SECRET,
        now=now,
    )

    # Verify at a time still within the TTL.
    result = verify(token, secret=_SECRET, now=now + 1)

    assert isinstance(result, VerifiedBinding)
    assert result.jti == jti
    assert result.tenant_id == tenant_id
    assert result.booking_id == booking_id
    assert result.exp == now + 3600


def test_mint_verify_round_trip_different_values_each_time() -> None:
    """Two distinct mints must produce tokens with distinct jti/tenant/booking."""
    t1 = mint(
        jti="jti-a", tenant_id=uuid4(), booking_id=uuid4(),
        ttl_seconds=3600, secret=_SECRET, now=_FIXED_NOW,
    )
    t2 = mint(
        jti="jti-b", tenant_id=uuid4(), booking_id=uuid4(),
        ttl_seconds=3600, secret=_SECRET, now=_FIXED_NOW,
    )
    assert t1 != t2

    r1 = verify(t1, secret=_SECRET, now=_FIXED_NOW + 1)
    r2 = verify(t2, secret=_SECRET, now=_FIXED_NOW + 1)
    assert r1.jti == "jti-a"
    assert r2.jti == "jti-b"
    assert r1.tenant_id != r2.tenant_id
    assert r1.booking_id != r2.booking_id


# ---------------------------------------------------------------------------
# Tampered payload / signature → BindingTokenInvalid
# ---------------------------------------------------------------------------


def test_tampered_payload_raises_invalid() -> None:
    """Changing even one byte of the payload segment must fail verification."""
    token = _mint_token()
    parts = token.split(".")
    assert len(parts) == 3  # v1.<payload>.<sig>

    # Decode payload, flip one field, re-encode.
    raw_payload = json.loads(_b64url_decode(parts[1]))
    raw_payload["jti"] = "attacker-controlled-jti"
    tampered_b64 = _b64url_encode(
        json.dumps(raw_payload, separators=(",", ":"), sort_keys=True).encode()
    )
    tampered_token = f"{parts[0]}.{tampered_b64}.{parts[2]}"

    with pytest.raises(BindingTokenInvalid):
        verify(tampered_token, secret=_SECRET, now=_FIXED_NOW + 1)


def test_tampered_signature_raises_invalid() -> None:
    """Replacing the signature segment must fail constant-time compare."""
    token = _mint_token()
    parts = token.split(".")
    # Append "X" to corrupt the signature.
    bad_sig = parts[2] + "X"
    tampered_token = f"{parts[0]}.{parts[1]}.{bad_sig}"

    with pytest.raises(BindingTokenInvalid):
        verify(tampered_token, secret=_SECRET, now=_FIXED_NOW + 1)


def test_wrong_secret_on_verify_raises_invalid() -> None:
    """A token minted with secret A must not verify under secret B."""
    token = _mint_token()  # minted with _SECRET
    with pytest.raises(BindingTokenInvalid):
        verify(token, secret="a-completely-different-secret", now=_FIXED_NOW + 1)


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_expired_token_raises_invalid() -> None:
    """Verifying at a time >= exp must raise BindingTokenInvalid."""
    token = mint(
        jti="exp-test",
        tenant_id=uuid4(),
        booking_id=uuid4(),
        ttl_seconds=3600,
        secret=_SECRET,
        now=_FIXED_NOW,
    )
    exp = _FIXED_NOW + 3600

    # Exactly at exp (current >= exp ⟹ expired).
    with pytest.raises(BindingTokenInvalid):
        verify(token, secret=_SECRET, now=exp)


def test_token_valid_one_second_before_expiry() -> None:
    """A token should verify successfully at exp - 1."""
    exp_offset = 3600
    token = mint(
        jti="just-in-time",
        tenant_id=uuid4(),
        booking_id=uuid4(),
        ttl_seconds=exp_offset,
        secret=_SECRET,
        now=_FIXED_NOW,
    )
    result = verify(token, secret=_SECRET, now=_FIXED_NOW + exp_offset - 1)
    assert result.jti == "just-in-time"


def test_zero_ttl_token_immediately_expired() -> None:
    """ttl_seconds=0 sets exp == now, so the token is expired at issue time."""
    token = mint(
        jti="zero-ttl",
        tenant_id=uuid4(),
        booking_id=uuid4(),
        ttl_seconds=0,
        secret=_SECRET,
        now=_FIXED_NOW,
    )
    with pytest.raises(BindingTokenInvalid):
        verify(token, secret=_SECRET, now=_FIXED_NOW)


# ---------------------------------------------------------------------------
# Empty / placeholder secret → BindingTokenConfigError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_secret", ["", "placeholder", "placeholder-binding-secret"])
def test_empty_or_placeholder_secret_on_mint_raises_config_error(
    bad_secret: str,
) -> None:
    with pytest.raises(BindingTokenConfigError):
        mint(
            jti="cfg-test",
            tenant_id=uuid4(),
            booking_id=uuid4(),
            ttl_seconds=3600,
            secret=bad_secret,
        )


@pytest.mark.parametrize("bad_secret", ["", "placeholder", "placeholder-binding-secret"])
def test_empty_or_placeholder_secret_on_verify_raises_config_error(
    bad_secret: str,
) -> None:
    # Build a validly-formed token (doesn't matter — config check runs first).
    token = _mint_token()
    with pytest.raises(BindingTokenConfigError):
        verify(token, secret=bad_secret, now=_FIXED_NOW + 1)


# ---------------------------------------------------------------------------
# Malformed token strings → BindingTokenInvalid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "",                                   # empty string
        "not-a-token",                        # no dots at all
        "v1.payload",                         # only 1 dot (2 segments)
        "v1.payload.sig.extra",               # 3 dots (4 segments)
        "v1..sig",                            # empty payload segment
    ],
)
def test_malformed_segment_count_raises_invalid(token: str) -> None:
    with pytest.raises(BindingTokenInvalid):
        verify(token, secret=_SECRET, now=_FIXED_NOW + 1)


def test_wrong_version_prefix_raises_invalid() -> None:
    token = _mint_token()
    parts = token.split(".")
    bad_version_token = f"v99.{parts[1]}.{parts[2]}"
    with pytest.raises(BindingTokenInvalid):
        verify(bad_version_token, secret=_SECRET, now=_FIXED_NOW + 1)


def test_bad_base64_payload_raises_invalid() -> None:
    """A payload segment that is invalid base64 must raise BindingTokenInvalid.

    We use a syntactically valid 3-part token but with garbage that passes
    signature (we re-sign the garbage payload with the real secret so the sig
    check passes and only the JSON decode fails).
    """
    import hashlib
    import hmac as _hmac

    garbage_b64 = "!!!not-base64!!!"
    # Re-sign the garbage so sig check passes and we reach JSON decode.
    key = _SECRET.encode("utf-8")
    sig = _b64url_encode(
        _hmac.new(key, garbage_b64.encode("ascii"), hashlib.sha256).digest()
    )
    bad_token = f"v1.{garbage_b64}.{sig}"
    with pytest.raises(BindingTokenInvalid):
        verify(bad_token, secret=_SECRET, now=_FIXED_NOW + 1)


def test_non_object_payload_raises_invalid() -> None:
    """A payload that is valid JSON but not a dict (e.g. a list) must raise."""
    import hashlib
    import hmac as _hmac

    payload_b64 = _b64url_encode(json.dumps([1, 2, 3]).encode("utf-8"))
    key = _SECRET.encode("utf-8")
    sig = _b64url_encode(
        _hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    )
    bad_token = f"v1.{payload_b64}.{sig}"
    with pytest.raises(BindingTokenInvalid):
        verify(bad_token, secret=_SECRET, now=_FIXED_NOW + 1)


def test_payload_missing_claims_raises_invalid() -> None:
    """Payload missing required keys (jti/tid/bid/exp) must raise."""
    import hashlib
    import hmac as _hmac

    incomplete = {"jti": "only-jti"}  # tid, bid, exp missing
    payload_b64 = _b64url_encode(
        json.dumps(incomplete, separators=(",", ":")).encode("utf-8")
    )
    key = _SECRET.encode("utf-8")
    sig = _b64url_encode(
        _hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    )
    bad_token = f"v1.{payload_b64}.{sig}"
    with pytest.raises(BindingTokenInvalid):
        verify(bad_token, secret=_SECRET, now=_FIXED_NOW + 1)


def test_non_uuid_tid_raises_invalid() -> None:
    """A ``tid`` value that is not a valid UUID must raise."""
    import hashlib
    import hmac as _hmac

    bad_payload = {
        "jti": "test",
        "tid": "not-a-uuid",
        "bid": str(uuid4()),
        "exp": _FIXED_NOW + 3600,
    }
    payload_b64 = _b64url_encode(
        json.dumps(bad_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    key = _SECRET.encode("utf-8")
    sig = _b64url_encode(
        _hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    )
    bad_token = f"v1.{payload_b64}.{sig}"
    with pytest.raises(BindingTokenInvalid):
        verify(bad_token, secret=_SECRET, now=_FIXED_NOW + 1)


# ---------------------------------------------------------------------------
# Settings integration: settings.booking_binding_secret (no secret= override)
# ---------------------------------------------------------------------------


def test_unconfigured_settings_secret_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the settings secret is empty/placeholder, mint without secret= override
    must raise BindingTokenConfigError (proves fail-closed posture)."""
    from polaris_api.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    original = settings.booking_binding_secret
    try:
        settings.booking_binding_secret = ""
        with pytest.raises(BindingTokenConfigError):
            mint(jti="x", tenant_id=uuid4(), booking_id=uuid4())
    finally:
        settings.booking_binding_secret = original
        get_settings.cache_clear()
