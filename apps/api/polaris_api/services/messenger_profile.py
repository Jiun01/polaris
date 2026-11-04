"""Meta Messenger Profile push — Phase 7 PR-5.

Wraps Meta Graph API ``/{version}/me/messenger_profile`` for the three
operations Polaris needs:

  * ``push_profile``         — POST the full bundle (Q-PHASE7-06) to Meta.
                                Re-GET after to detect partial-success
                                (Meta returns 200 even when a subset of
                                fields didn't actually apply, e.g. an
                                icebreaker dropped for a locale rule).
  * ``get_profile``          — GET ?fields=... to read current Meta state.
                                Used by the portal "out-of-sync" check.
  * ``delete_profile_fields`` — DELETE ?fields=... to clear specific
                                sections. Required when a tenant clears
                                icebreakers entirely — Meta's POST is
                                merge-only, so sending an empty list
                                doesn't clear the section.

URL pattern: ``/{version}/me/messenger_profile``. Meta resolves ``me``
from the access token so no page_id segment is required — cleaner for
our multi-tenant model where each tenant has their own Page + token.

Authentication: Page access token from ``tenant_secrets`` (kind
``meta_fb_page_access_token``). Passed as ``access_token`` query param
(Meta's convention) so it doesn't end up in the Authorization header
where redactors might miss it.

Error taxonomy mirrors ChatwootClient + meta_whatsapp.py:
  - ``MessengerProfileTransientError``  — 5xx / timeout / connection
  - ``MessengerProfileStructuralError`` — 4xx (token revoked, invalid
                                          payload, missing perms)
  - ``MessengerProfileConfigError``     — channel row missing, fb_page_id
                                          NULL, token absent in
                                          tenant_secrets (fail-closed
                                          before any HTTP)

Doc sources (verified 2026-05):
  - https://developers.facebook.com/docs/messenger-platform/reference/messenger-profile-api/
  - https://developers.facebook.com/blog/post/2026/02/18/introducing-graph-api-v25-and-marketing-api-v25/
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final
from uuid import UUID

import asyncpg
import httpx
from polaris_shared_types.messenger_profile import MessengerProfilePushResult

from polaris_api.config import get_settings

logger = logging.getLogger(__name__)


# tenant_secrets kind under which the per-Page access token lives.
# Matches Phase 1 secret-kind vocabulary (paste-in flow in admin tooling).
_PAGE_TOKEN_KIND: Final[str] = "meta_fb_page_access_token"  # noqa: S105

# The three bundle sections Polaris supports at GA per 04h §4.2.
# Other Meta fields (greeting, whitelisted_domains, account_linking_url,
# commands) are out-of-scope at GA — extending the bundle adds them here.
_FIELDS_PUSHED: Final[tuple[str, ...]] = (
    "ice_breakers",
    "persistent_menu",
    "get_started",
)


# =====================================================================
# Error taxonomy
# =====================================================================


class MessengerProfileError(RuntimeError):
    """Base class. Carries ``status_code`` when available."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MessengerProfileConfigError(MessengerProfileError):
    """Channel row missing, fb_page_id NULL, or page token absent.

    Fail-closed BEFORE any HTTP. Surfaced to the portal so tenants can
    fix the channel config before re-pushing.
    """


class MessengerProfileTransientError(MessengerProfileError):
    """5xx / timeout / connection error. Caller may retry; write-back
    stamps ``messenger_profile_last_push_error='meta_5xx_after_retries'``
    so the portal can show the failure mode."""


class MessengerProfileStructuralError(MessengerProfileError):
    """4xx response. Typical: token revoked (401), invalid payload (400),
    missing pages_messaging permission (403). No retry — operator action
    required."""


def _classify_error_message(status_code: int) -> str:
    """Map status_code to the vocabulary stored in
    ``tenant_channels.messenger_profile_last_push_error`` (matches
    MessengerProfilePushResult.error documented vocabulary)."""
    if status_code == 401:
        return "meta_401_token_revoked"
    if status_code == 403:
        return "meta_403_forbidden"
    if status_code == 400:
        return "meta_400_invalid_payload"
    if status_code >= 500:
        return "meta_5xx_after_retries"
    return f"meta_{status_code}"


def _parse_meta_error_message(response: httpx.Response) -> str:
    """Extract ``error.message`` from Meta's structured envelope.

    Used in log lines + exception messages. Mirrors ``_parse_meta_error``
    in services/meta_whatsapp.py — Meta uses the same envelope shape
    across product APIs.

    Capped at 200 chars before returning (security-reviewer Nit 1) — the
    extracted string is forwarded into HTTPException detail and surfaced
    to portal clients. Meta has historically included partial token
    suffixes / page identifiers in error envelopes under some 190/102
    codes, so the cap is defence-in-depth.
    """
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str) and msg:
                return msg[:200]
    return f"HTTP {response.status_code}"


# =====================================================================
# DB helpers
# =====================================================================


_LOAD_CHANNEL_SQL = """
SELECT
    fb_page_id                       AS fb_page_id,
    messenger_icebreakers            AS messenger_icebreakers,
    messenger_persistent_menu        AS messenger_persistent_menu,
    messenger_get_started            AS messenger_get_started
FROM tenant_channels
WHERE id = $1 AND tenant_id = $2
"""


_WRITE_BACK_SUCCESS_SQL = """
UPDATE tenant_channels
   SET messenger_profile_last_pushed_at  = NOW(),
       messenger_profile_last_push_error = NULL
 WHERE id = $1 AND tenant_id = $2
"""


_WRITE_BACK_FAILURE_SQL = """
UPDATE tenant_channels
   SET messenger_profile_last_push_error = $3
 WHERE id = $1 AND tenant_id = $2
"""


def _normalise_jsonb(value: object) -> Any:
    """asyncpg returns JSONB as str OR dict/list depending on conn init.
    Normalise to native Python types.

    silent-failure-hunter LOW: log at WARNING when json.loads fails so
    operators can investigate why a tenant's icebreakers disappeared.
    Direct DB writes are the only realistic path to corrupt JSONB here,
    but the operator-visibility cost is one log line — worth paying.
    """
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            logger.warning(
                "messenger_profile: corrupt JSONB column (first 100 chars): %r",
                value[:100],
            )
            return None
    return None


async def _load_channel_config(
    conn: asyncpg.Connection, *, tenant_id: UUID, tenant_channel_id: UUID
) -> dict[str, Any]:
    """Load the channel row + raise ConfigError if missing / unconfigured.

    Returns a dict with normalised JSONB columns and the page id string.
    """
    row = await conn.fetchrow(_LOAD_CHANNEL_SQL, tenant_channel_id, tenant_id)
    if row is None:
        raise MessengerProfileConfigError(
            f"tenant_channels row not found: tenant_id={tenant_id} "
            f"channel_id={tenant_channel_id}"
        )
    fb_page_id = row["fb_page_id"]
    if not isinstance(fb_page_id, str) or not fb_page_id:
        raise MessengerProfileConfigError(
            "fb_page_id missing on tenant_channels row — channel not "
            "connected to a Facebook Page yet"
        )
    return {
        "fb_page_id": fb_page_id,
        "ice_breakers": _normalise_jsonb(row["messenger_icebreakers"]) or [],
        "persistent_menu": _normalise_jsonb(row["messenger_persistent_menu"]) or [],
        "get_started": _normalise_jsonb(row["messenger_get_started"]),
    }


async def _load_page_token(
    conn: asyncpg.Connection, tenant_id: UUID
) -> str:
    """Decrypt the per-tenant Facebook Page access token.

    Page tokens live in tenant_secrets under ``meta_fb_page_access_token``
    per Phase 1's secret-kind vocabulary. The portal Channels screen
    writes them on Page connect.
    """
    from polaris_api.services.secrets import get as secrets_get

    token_bytes = await secrets_get(conn, tenant_id, _PAGE_TOKEN_KIND)
    if token_bytes is None:
        raise MessengerProfileConfigError(
            f"meta_fb_page_access_token not configured for tenant_id={tenant_id}"
        )
    try:
        return token_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MessengerProfileConfigError(
            f"meta_fb_page_access_token decode failed for tenant_id={tenant_id}"
        ) from exc


def _build_bundle(channel: dict[str, Any]) -> dict[str, Any]:
    """Assemble the full bundle body Meta expects.

    Q-PHASE7-06 always-PUT-full: include all three sections every call.
    ``get_started`` is omitted when None (Meta treats absent + null
    differently; absent = "no change", explicit null = "clear" which is
    NOT what we want — that's the DELETE path). ``ice_breakers`` /
    ``persistent_menu`` ship as empty lists when unset.
    """
    body: dict[str, Any] = {
        "ice_breakers": channel["ice_breakers"],
        "persistent_menu": channel["persistent_menu"],
    }
    if channel["get_started"] is not None:
        body["get_started"] = channel["get_started"]
    return body


def _compute_diff(sent: dict[str, Any], remote: dict[str, Any]) -> dict[str, Any] | None:
    """Compare what we POSTed vs what Meta returned on re-GET.

    Returns None when everything matches (clean push). Returns a dict
    keyed by section name with the divergence when Meta dropped or
    transformed any field. Drives the portal "out-of-sync" banner.

    Comparison is structural — we don't reorder lists or normalise
    nested whitespace because Meta preserves order in its responses.
    """
    diff: dict[str, Any] = {}
    for key in ("ice_breakers", "persistent_menu", "get_started"):
        sent_val = sent.get(key)
        remote_val = remote.get(key)
        if sent_val is None and remote_val is None:
            continue
        if sent_val != remote_val:
            diff[key] = {"sent": sent_val, "remote": remote_val}
    return diff or None


# =====================================================================
# HTTP execution
# =====================================================================


async def _request_meta(
    *,
    method: str,
    version: str,
    access_token: str,
    timeout_s: float,
    json_body: dict[str, Any] | None = None,
    extra_params: dict[str, str] | None = None,
) -> httpx.Response:
    """Issue one HTTP request to /{version}/me/messenger_profile.

    No retry budget — Meta's product-API surface is typically reliable
    enough that retries are caller-driven (portal can re-fire push if
    the operator wants). Keeping a single attempt also bounds the wall
    time of any single push to ``MESSENGER_PROFILE_API_TIMEOUT_S`` so
    the portal HTTP handler doesn't hang on a Meta outage.
    """
    url = f"https://graph.facebook.com/{version}/me/messenger_profile"
    params: dict[str, str] = {"access_token": access_token}
    if extra_params:
        params.update(extra_params)
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            return await client.request(
                method,
                url,
                json=json_body,
                params=params,
            )
    except httpx.TimeoutException as exc:
        raise MessengerProfileTransientError(
            f"Meta /messenger_profile timed out: {exc}"
        ) from exc
    except httpx.ConnectError as exc:
        raise MessengerProfileTransientError(
            f"Meta /messenger_profile connect error: {exc}"
        ) from exc


def _raise_for_status(response: httpx.Response, *, op: str) -> None:
    """Map non-2xx to our taxonomy. 4xx → structural, 5xx → transient."""
    if response.status_code < 400:
        return
    message = _parse_meta_error_message(response)
    if response.status_code >= 500:
        logger.warning(
            "messenger_profile %s 5xx: status=%d msg=%r",
            op,
            response.status_code,
            message,
        )
        raise MessengerProfileTransientError(
            message, status_code=response.status_code
        )
    logger.warning(
        "messenger_profile %s 4xx: status=%d msg=%r",
        op,
        response.status_code,
        message,
    )
    raise MessengerProfileStructuralError(
        message, status_code=response.status_code
    )


# =====================================================================
# Public API
# =====================================================================


async def push_profile(
    *,
    tenant_id: UUID,
    tenant_channel_id: UUID,
    conn: asyncpg.Connection,
) -> MessengerProfilePushResult:
    """POST the full Messenger Profile bundle + diff-check via re-GET.

    On Meta 4xx/5xx: stamps ``messenger_profile_last_push_error`` and
    raises the classified exception. On Meta 2xx: stamps
    ``messenger_profile_last_pushed_at`` and clears the error column.

    Returns ``MessengerProfilePushResult`` with ``diff_against_remote``
    populated when re-GET shows our send didn't fully apply (Meta
    silently dropped fields for locale / policy reasons).
    """
    settings = get_settings()
    channel = await _load_channel_config(
        conn, tenant_id=tenant_id, tenant_channel_id=tenant_channel_id
    )
    access_token = await _load_page_token(conn, tenant_id)
    bundle = _build_bundle(channel)
    pushed_at_iso: str

    # ----- POST -----
    try:
        post_resp = await _request_meta(
            method="POST",
            version=settings.meta_graph_api_version,
            access_token=access_token,
            timeout_s=settings.messenger_profile_api_timeout_s,
            json_body=bundle,
        )
        _raise_for_status(post_resp, op="push")
    except (
        MessengerProfileTransientError,
        MessengerProfileStructuralError,
    ) as exc:
        # Stamp last_push_error before re-raising so the portal sees
        # the failure even on transient classes.
        await conn.execute(
            _WRITE_BACK_FAILURE_SQL,
            tenant_channel_id,
            tenant_id,
            _classify_error_message(exc.status_code or 599),
        )
        raise

    # Capture pushed_at timestamp now (before re-GET) so even if re-GET
    # fails the success window is honestly recorded.
    from datetime import UTC, datetime

    pushed_at_iso = datetime.now(UTC).isoformat()

    # ----- Re-GET for partial-success detection -----
    diff: dict[str, Any] | None = None
    verification_skipped = False
    try:
        get_resp = await _request_meta(
            method="GET",
            version=settings.meta_graph_api_version,
            access_token=access_token,
            timeout_s=settings.messenger_profile_api_timeout_s,
            extra_params={"fields": ",".join(_FIELDS_PUSHED)},
        )
        if get_resp.status_code == 200:
            payload = get_resp.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, list) and data:
                remote_state = data[0] if isinstance(data[0], dict) else {}
                diff = _compute_diff(bundle, remote_state)
        else:
            verification_skipped = True
    except MessengerProfileTransientError:
        # Re-GET failed — push succeeded but we can't verify Meta's
        # persisted state. silent-failure-hunter MEDIUM: promote to
        # WARNING so this surfaces in ops dashboards (filter ≥WARNING),
        # and signal the portal via diff_against_remote so it can show
        # an "unverified" badge distinct from a clean push.
        verification_skipped = True
        logger.warning(
            "messenger_profile push re-GET failed (verification skipped — "
            "Meta got our push but we cannot confirm persisted state) "
            "tenant_id=%s channel=%s",
            tenant_id,
            tenant_channel_id,
        )

    if verification_skipped and diff is None:
        # Distinguish "verified clean" (diff=None) from "unverified"
        # so the portal can render different banners.
        diff = {"_verification": "skipped"}

    # ----- Write-back success -----
    # silent-failure-hunter LOW: guard write-back so a DB hiccup after
    # a successful Meta push doesn't surface as a 500 to the portal.
    # The push DID succeed — Meta has the bundle. Worst-case the
    # portal keeps showing "needs push" until the next successful
    # write-back, which is acceptable consistency.
    try:
        await conn.execute(_WRITE_BACK_SUCCESS_SQL, tenant_channel_id, tenant_id)
    except Exception:  # noqa: BLE001 — push already succeeded; don't poison the response
        logger.warning(
            "messenger_profile push succeeded but write-back failed "
            "tenant_id=%s channel=%s — portal may keep showing 'needs push'",
            tenant_id,
            tenant_channel_id,
        )

    # MessengerProfilePushResult.fields_pushed is typed as
    # list[Literal['ice_breakers','persistent_menu','get_started']];
    # mypy can't narrow a tuple[str, ...] there, so build the list
    # element-by-element to satisfy the Literal arg.
    return MessengerProfilePushResult(
        success=True,
        pushed_at=pushed_at_iso,
        fields_pushed=["ice_breakers", "persistent_menu", "get_started"],
        diff_against_remote=diff,
        error=None,
    )


async def get_profile(
    *,
    tenant_id: UUID,
    tenant_channel_id: UUID,
    fields: list[str] | None,
    conn: asyncpg.Connection,
) -> dict[str, Any]:
    """GET /{version}/me/messenger_profile?fields=...

    Returns the inner ``data[0]`` dict from Meta (the shape tenants
    actually care about) or ``{}`` when Meta returns an empty list.
    """
    settings = get_settings()
    await _load_channel_config(
        conn, tenant_id=tenant_id, tenant_channel_id=tenant_channel_id
    )
    access_token = await _load_page_token(conn, tenant_id)

    extra: dict[str, str] = {}
    if fields:
        extra["fields"] = ",".join(fields)
    response = await _request_meta(
        method="GET",
        version=settings.meta_graph_api_version,
        access_token=access_token,
        timeout_s=settings.messenger_profile_api_timeout_s,
        extra_params=extra,
    )
    _raise_for_status(response, op="get")
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


async def delete_profile_fields(
    *,
    tenant_id: UUID,
    tenant_channel_id: UUID,
    fields: list[str],
    conn: asyncpg.Connection,
) -> None:
    """DELETE /{version}/me/messenger_profile?fields=ice_breakers,...

    Required to clear a section — Meta's POST is merge-only and
    sending an empty list doesn't clear (Meta treats empty-list as
    "no change" for some sections). Tenants who clear their
    icebreaker config trigger this from the portal.

    NB: this does NOT touch our DB columns. The router pairs this with
    a DB update if the tenant cleared the config in the portal.
    """
    if not fields:
        return
    settings = get_settings()
    await _load_channel_config(
        conn, tenant_id=tenant_id, tenant_channel_id=tenant_channel_id
    )
    access_token = await _load_page_token(conn, tenant_id)

    response = await _request_meta(
        method="DELETE",
        version=settings.meta_graph_api_version,
        access_token=access_token,
        timeout_s=settings.messenger_profile_api_timeout_s,
        extra_params={"fields": ",".join(fields)},
    )
    _raise_for_status(response, op="delete")


__all__ = [
    "MessengerProfileConfigError",
    "MessengerProfileError",
    "MessengerProfileStructuralError",
    "MessengerProfileTransientError",
    "delete_profile_fields",
    "get_profile",
    "push_profile",
]
