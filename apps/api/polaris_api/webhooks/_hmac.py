"""Constant-time HMAC verification for inbound webhook signatures.

Two distinct schemes share this module — they share the SHA-256 keyed-HMAC
primitive but differ in header format and prefix:

- **Meta** (WhatsApp Cloud API, Instagram, Messenger): header
  ``X-Hub-Signature-256`` carries ``sha256=<hex>``. The HMAC is computed
  over the raw request body using the platform-level ``META_APP_SECRET``.

- **Chatwoot** (Polaris fork, Agent Bot): header ``X-Chatwoot-Signature``
  carries ``sha256=<hex>`` where the HMAC is computed over
  ``"{X-Chatwoot-Timestamp}.{raw_body}"`` (a Stripe-style timestamp-prefixed
  scheme with replay protection). A bare-hex header + raw-body-only HMAC is
  still accepted when no timestamp is forwarded (legacy back-compat). The
  shared secret is provisioned per Chatwoot install (one secret for GA;
  per-inbox rotation is Phase 8 portal work).

Phase 2 PR-1 ships the Meta verifier; PR-5 adds the Chatwoot verifier.

Security invariants (every code path here must honour):

1. **Constant-time compare** — uses ``hmac.compare_digest``. A bare ``==``
   would leak timing side-channels that let an attacker recover the
   secret one nibble at a time.
2. **No raises into the request path** — malformed/missing header returns
   ``False``; the router maps that to HTTP 401. Raising would convert a
   401 into a 500 and leak stack traces.
3. **Verify over raw bytes** — the caller MUST pass the unmodified body
   bytes from ``await request.body()``. Re-serialising via
   ``json.dumps(await request.json())`` would change whitespace + key
   ordering and break the compare.

Exit criterion 1 of Phase 2 (per checklist + 04c §10) is satisfied by the
``hmac.compare_digest`` call in ``_compare``.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterable

_META_HEADER_PREFIX = "sha256="


def _compute_hex(raw: bytes, secret: str) -> str:
    """SHA-256 HMAC over ``raw`` keyed by ``secret``. Returns lower-case hex."""
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def _compare(expected_hex: str, provided_hex: str) -> bool:
    """Constant-time compare. Never raises.

    ``hmac.compare_digest`` raises ``TypeError`` if the two inputs are of
    different types (``str`` vs ``bytes``). Both inputs here are ``str``,
    but if a future refactor passes ``bytes`` for one side, the type guard
    catches it without exposing the failure to the request path.
    """
    if not isinstance(expected_hex, str) or not isinstance(provided_hex, str):
        return False
    return hmac.compare_digest(expected_hex, provided_hex)


def verify_meta_signature(raw: bytes, header: str | None, secret: str) -> bool:
    """Verify Meta's ``X-Hub-Signature-256`` over ``raw``.

    Returns ``True`` only when:
      - ``secret`` is non-empty (an empty secret would let a missing header
        + missing payload validate as "matching empties", which is a
        silent bypass; we treat empty-secret as misconfiguration and fail
        closed).
      - ``header`` is present and starts with ``sha256=``.
      - The hex after ``sha256=`` matches ``HMAC-SHA256(secret, raw)``
        under constant-time compare.

    Any other state returns ``False`` (router → 401). Never raises.
    """
    if not secret:
        # Misconfiguration. Fail closed; the router logs a high-severity line.
        return False
    if not header or not header.startswith(_META_HEADER_PREFIX):
        return False
    provided_hex = header[len(_META_HEADER_PREFIX):]
    expected_hex = _compute_hex(raw, secret)
    return _compare(expected_hex, provided_hex)


def verify_meta_signature_multi(
    raw: bytes, header: str | None, secrets: Iterable[str]
) -> bool:
    """Verify Meta's ``X-Hub-Signature-256`` against ANY of ``secrets``.

    Used by the unified ``/v1/webhooks/meta`` dispatcher, which receives
    ``object="instagram"`` for BOTH the FB-page-linked Instagram channel
    (signed with ``META_APP_SECRET``) and the Instagram-API-with-Instagram-Login
    channel (signed with a separate Instagram product App Secret). HMAC verify
    must happen over the raw body **before** the payload is parsed/resolved, so
    we cannot yet know which channel the delivery belongs to — hence we try each
    candidate secret and accept if any matches.

    Each candidate is checked with the same fail-closed, constant-time posture
    as ``verify_meta_signature`` (empty secrets skipped; header must start with
    ``sha256=``). Returns ``True`` on the first match, ``False`` if none match.
    Never raises.

    Security note: trying N secrets multiplies the per-request HMAC cost by N
    (here N=2) and gives a forger N independent chances, but every candidate is
    a high-entropy secret we control, so this is a negligible widening — the
    same property that makes a single 256-bit HMAC secure. We still compare
    every candidate in constant time (no early-exit on length/prefix that would
    leak which secret was closer).
    """
    if not header or not header.startswith(_META_HEADER_PREFIX):
        return False
    provided_hex = header[len(_META_HEADER_PREFIX):]
    matched = False
    for secret in secrets:
        if not secret:
            # Misconfigured / unset candidate — skip without short-circuiting
            # the loop, so an empty secret can never produce a silent bypass.
            continue
        expected_hex = _compute_hex(raw, secret)
        # Accumulate rather than early-return so the number of compare_digest
        # calls does not depend on which secret (if any) matched.
        if _compare(expected_hex, provided_hex):
            matched = True
    return matched


def verify_chatwoot_signature(
    raw: bytes,
    header: str | None,
    secret: str,
    timestamp: str | None = None,
    max_skew_seconds: int = 300,
) -> bool:
    """Verify the Polaris Chatwoot fork's ``X-Chatwoot-Signature`` over ``raw``.

    The fork signs ``sha256=<hex>`` where ``hex`` is
    ``HMAC-SHA256(secret, "{X-Chatwoot-Timestamp}.{raw_body}")`` — i.e. a
    timestamp-prefixed scheme (Stripe-style) with replay protection. Two
    distinctions remain relative to Meta:

      1. **Header** — ``X-Chatwoot-Signature`` (the fork additionally emits a
         leading ``sha256=`` prefix, which we strip; a bare-hex header is also
         accepted for back-compat).
      2. **Per-install secret** — at GA we use one
         ``CHATWOOT_WEBHOOK_HMAC_SECRET`` for the whole Polaris-managed
         Chatwoot fork; per-inbox rotation lands with the Phase 8 portal
         (Q-PHASE2-01 in 08_OPEN_QUESTIONS.md).

    Signing scheme by ``timestamp``:
      * ``timestamp`` provided (the fork's contract) — reject when it is not a
        parseable int or is more than ``max_skew_seconds`` from the current
        wall clock (replay protection), then compute the HMAC over
        ``"{timestamp}.".encode() + raw``.
      * ``timestamp`` is ``None`` — legacy bare-hex-over-raw computation, kept
        for back-compat with callers that don't forward the timestamp header.

    Same fail-closed posture as the Meta verifier: empty secret, missing
    header, or a stale/unparseable timestamp → ``False`` (router → 401).
    Constant-time compare. Never raises.
    """
    if not secret:
        return False
    if not header:
        return False
    # The fork prefixes the hex with "sha256="; strip it if present. A bare-hex
    # header (legacy) is accepted unchanged.
    provided_hex = header
    if provided_hex.startswith(_META_HEADER_PREFIX):
        provided_hex = provided_hex[len(_META_HEADER_PREFIX):]

    if timestamp is not None:
        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            return False
        if abs(time.time() - ts) > max_skew_seconds:
            return False
        expected_hex = _compute_hex(f"{timestamp}.".encode() + raw, secret)
    else:
        expected_hex = _compute_hex(raw, secret)

    return _compare(expected_hex, provided_hex)
