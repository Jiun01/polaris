"""Unit tests for ``verify_chatwoot_signature`` (channel-recovery B7).

DB-free, direct-function tests. The Polaris Chatwoot fork signs
``X-Chatwoot-Signature: sha256=<hex>`` where
``hex = HMAC-SHA256(secret, "{X-Chatwoot-Timestamp}.{raw_body}")``. The
verifier also keeps a legacy bare-hex-over-raw mode when no timestamp is
forwarded.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from polaris_api.webhooks._hmac import verify_chatwoot_signature

_SECRET = "channel-recovery-b7-secret"
_BODY = b'{"event":"message_created","id":1}'


def _sign(body: bytes, secret: str, timestamp: str | None) -> str:
    msg = f"{timestamp}.".encode() + body if timestamp is not None else body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def _fresh_ts() -> str:
    return str(int(time.time()))


def test_valid_timestamped_signature_with_prefix() -> None:
    ts = _fresh_ts()
    header = f"sha256={_sign(_BODY, _SECRET, ts)}"
    assert verify_chatwoot_signature(_BODY, header, _SECRET, timestamp=ts) is True


def test_valid_timestamped_signature_bare_hex() -> None:
    """A bare-hex header (no prefix) over the timestamped scheme still passes."""
    ts = _fresh_ts()
    header = _sign(_BODY, _SECRET, ts)
    assert verify_chatwoot_signature(_BODY, header, _SECRET, timestamp=ts) is True


def test_legacy_no_timestamp_raw_hmac() -> None:
    """Back-compat: when no timestamp is forwarded, HMAC is over the raw body."""
    header = f"sha256={_sign(_BODY, _SECRET, None)}"
    assert verify_chatwoot_signature(_BODY, header, _SECRET) is True
    assert verify_chatwoot_signature(_BODY, _sign(_BODY, _SECRET, None), _SECRET) is True


def test_wrong_secret_fails() -> None:
    ts = _fresh_ts()
    header = f"sha256={_sign(_BODY, 'other-secret', ts)}"
    assert verify_chatwoot_signature(_BODY, header, _SECRET, timestamp=ts) is False


def test_empty_secret_fails_closed() -> None:
    ts = _fresh_ts()
    header = f"sha256={_sign(_BODY, _SECRET, ts)}"
    assert verify_chatwoot_signature(_BODY, header, "", timestamp=ts) is False


def test_missing_header_fails_closed() -> None:
    assert verify_chatwoot_signature(_BODY, None, _SECRET, timestamp=_fresh_ts()) is False


def test_stale_timestamp_rejected() -> None:
    stale = str(int(time.time()) - 10_000)
    header = f"sha256={_sign(_BODY, _SECRET, stale)}"
    assert verify_chatwoot_signature(_BODY, header, _SECRET, timestamp=stale) is False


def test_future_timestamp_rejected() -> None:
    future = str(int(time.time()) + 10_000)
    header = f"sha256={_sign(_BODY, _SECRET, future)}"
    assert verify_chatwoot_signature(_BODY, header, _SECRET, timestamp=future) is False


def test_unparseable_timestamp_rejected() -> None:
    header = f"sha256={_sign(_BODY, _SECRET, 'not-an-int')}"
    assert (
        verify_chatwoot_signature(_BODY, header, _SECRET, timestamp="not-an-int")
        is False
    )


def test_timestamp_within_custom_skew_passes() -> None:
    near = str(int(time.time()) - 60)
    header = f"sha256={_sign(_BODY, _SECRET, near)}"
    assert (
        verify_chatwoot_signature(
            _BODY, header, _SECRET, timestamp=near, max_skew_seconds=120
        )
        is True
    )
