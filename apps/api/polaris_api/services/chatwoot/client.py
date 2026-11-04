"""Chatwoot REST API client — Phase 7 PR-2.

Adapter over the Chatwoot REST API v1 (v4.13.0 contract, per the
polaris-cw-fork). Pure HTTP — does NOT read the database. Per-tenant
scoping comes from the ``account_id`` argument on every call (resolved
from ``tenants.chatwoot_account_id`` at the caller boundary —
``services/chatwoot/handoff.py`` and friends).

Six verbs exposed (per 04h_PHASE_7_DEEP.md §5.1):

  - ``flip_conversation_status``  — POST /toggle_status (used to flip → open on handoff)
  - ``assign_team``                — POST /assignments (team routing)
  - ``post_private_note``          — POST /messages with private=True
  - ``apply_conversation_label``   — POST /conversations/{id}/labels
  - ``apply_contact_label``        — POST /contacts/{id}/labels
  - ``ensure_label``               — GET → POST /labels idempotent create

Authentication: SINGLE platform-wide token per Q-PHASE7-01. Chatwoot's
``agent_bots`` table issues a global token that attaches across all
accounts via super-admin; URL composition per-call carries the tenant's
``account_id``. The token comes from ``settings.chatwoot_bot_access_token``;
no per-tenant ``tenant_secrets`` lookup.

Retry policy per ADR-051 + 04h §5.1:

  - 5xx / timeout / connection-reset → ``ChatwootTransientError`` with
    up to ``CHATWOOT_API_MAX_RETRIES`` retries (exponential backoff
    with full jitter: 0.5s + 1.0s for the default of 2 retries).
    Caller (handoff service) maps the final exception to
    ``HandoffOutcome.DECLINED`` — the customer-facing handoff message
    still went out via the channel dispatcher; the Chatwoot mirror
    failed transiently and recovery-on-next-inbound covers (Q-PHASE7-04).

  - 4xx (any) → ``ChatwootStructuralError``, NO retry. Caller maps to
    ``HandoffOutcome.ERRORED``. Typical cause: team_id doesn't exist,
    conversation already closed, conversation_id wrong for this account.

Configuration errors (empty token, placeholder token, missing
base_url) raise ``ChatwootConfigError`` — a hard fail-closed posture
mirroring ``meta_whatsapp.py``'s ``_resolve_token`` discipline. Better
to crash loudly at first call than to silently leak the placeholder
to Chatwoot's auth layer.

Pattern parity: file shape mirrors ``services/meta_whatsapp.py``
(custom errors → helpers → client class → ``@lru_cache`` factory →
test reset). Retry is the one structural deviation — meta_whatsapp.py
delegates retry to the broadcast worker layer; Chatwoot integration
has multiple callers (handoff, label_sync, future booking webhook
listener) so the budget belongs INSIDE the client to avoid forking
the retry logic across three callsites.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any, Final
from urllib.parse import quote

import httpx

from polaris_api import http_client
from polaris_api.config import get_settings

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def _get_client(fallback_timeout: float) -> AsyncIterator[httpx.AsyncClient]:
    """Shared-or-fresh httpx client (audit Batch B / B6).

    Prefers the lifespan-scoped singleton (prod) — every request through it
    still passes its own explicit ``timeout=``/``follow_redirects=``, so
    behavior is identical to a fresh per-call client. Falls back to a fresh
    ``httpx.AsyncClient(timeout=fallback_timeout)`` — the previous behaviour
    — when the singleton isn't initialised (unit tests / scripts). Never
    closes the shared client (only the lifespan does).
    """
    shared = http_client.get_http_client_or_none()
    if shared is not None:
        yield shared
    else:
        async with httpx.AsyncClient(timeout=fallback_timeout) as client:
            yield client

# Label colour used when ensure_label creates a new label. Chatwoot's
# label-create endpoint requires a color; we pick a deterministic value
# so two tenants' provisioning runs don't produce visually-different
# pol_opted_out labels. Hex matches Polaris brand secondary.
_DEFAULT_LABEL_COLOR: Final[str] = "#6B7280"


# =====================================================================
# Error taxonomy
# =====================================================================


class ChatwootError(RuntimeError):
    """Base class for all Chatwoot client errors.

    Carries ``status_code`` so callers can branch on the HTTP class even
    when they don't subclass-match. The retry loop relies on the
    distinction between ``ChatwootTransientError`` and
    ``ChatwootStructuralError``; this base exists so callers that
    don't care about retry semantics can catch a single type.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ChatwootConfigError(ChatwootError):
    """Bot token unset / placeholder, or base_url missing.

    Fail-closed — refuse to issue any HTTP at all rather than dispatch
    with garbage credentials.
    """


class ChatwootTransientError(ChatwootError):
    """5xx, network timeout, connection-reset — retry budget exhausted.

    Per ADR-051: caller maps this to ``HandoffOutcome.DECLINED`` (flow
    false-branch with TRANSIENT semantics). The customer-facing message
    still went out; the Chatwoot mirror is what failed. Recovery on
    next inbound covers via ``conversations.handoff_pending_recovery_at``.
    """


class ChatwootStructuralError(ChatwootError):
    """4xx response — NO retry, caller's bug or stale config.

    Per ADR-051: caller maps this to ``HandoffOutcome.ERRORED``. Typical:
    team_id doesn't exist in the target Chatwoot account, conversation
    already closed, contact_id doesn't belong to the bot's inbox.

    ``message`` carries the Chatwoot error envelope verbatim when one is
    present. ``response_body`` retains the raw body for diagnosis.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.response_body = response_body


def _parse_chatwoot_error(response: httpx.Response, *, default: str) -> str:
    """Best-effort extraction of Chatwoot's error envelope.

    Chatwoot returns ``{message: "..."}`` for most 4xx; some endpoints
    use ``{errors: ["..."]}``. We try both before falling back to the
    default. Does NOT raise — purely informational; the raise happens in
    the caller after we attach the parsed message.
    """
    try:
        body = response.json()
    except ValueError:
        return default
    if isinstance(body, dict):
        msg = body.get("message")
        if isinstance(msg, str) and msg:
            return msg
        errs = body.get("errors")
        if isinstance(errs, list) and errs and isinstance(errs[0], str):
            return errs[0]
    return default


# =====================================================================
# Client
# =====================================================================


class ChatwootClient:
    """Async HTTP client for the Chatwoot REST API v1.

    Constructed lazily via ``get_chatwoot_client()`` (lru_cache singleton).
    Tests monkeypatch the factory after ``reset_client_for_testing()``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        bot_access_token: str,
        timeout_s: float,
        max_retries: int,
        label_auto_provision: bool,
        agent_api_token: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.bot_access_token = bot_access_token
        self.agent_api_token = agent_api_token
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.label_auto_provision = label_auto_provision

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _check_config(self) -> None:
        """Fail-closed on placeholder / empty config.

        Raised at the start of every public verb so a misconfigured
        deploy can't accidentally dispatch real traffic with a junk
        token (which would either 401 noisily or — worse — succeed if
        the placeholder happened to match a real token).
        """
        if not self.base_url:
            raise ChatwootConfigError("CHATWOOT_BASE_URL is unset.")
        if not self.bot_access_token or self.bot_access_token.startswith(
            "placeholder-"
        ):
            raise ChatwootConfigError(
                "CHATWOOT_BOT_ACCESS_TOKEN is unset or placeholder. "
                "Refusing to dispatch. Paste the platform agent_bots "
                "token into `.env` (created via Chatwoot super-admin "
                "→ Agent Bots) before invoking the handoff flow."
            )

    def _check_user_config(self) -> None:
        """Fail-closed check for endpoints that require a user-level token.

        CW agent_bot tokens are rejected with 401 for contacts/conversation
        create endpoints.  A regular agent/admin token from /auth/sign_in is
        required.  Raises ChatwootConfigError if not set so callers can catch
        and skip gracefully.
        """
        if not self.base_url:
            raise ChatwootConfigError("CHATWOOT_BASE_URL is unset.")
        if not self.agent_api_token:
            raise ChatwootConfigError(
                "CHATWOOT_AGENT_API_TOKEN is unset. "
                "Set it to a CW agent/admin user token to enable contact "
                "and conversation creation."
            )

    def _headers(self) -> dict[str, str]:
        """Auth + content-type header pair used by bot-token verbs."""
        return {
            "api_access_token": self.bot_access_token,
            "Content-Type": "application/json",
        }

    def _user_headers(self) -> dict[str, str]:
        """Auth header pair for user-token verbs (contacts, conversations create)."""
        return {
            "api_access_token": self.agent_api_token,
            "Content-Type": "application/json",
        }

    def _user_checked_headers(self) -> dict[str, str]:
        """Fail-closed config check + the user-token header pair.

        Combines ``_check_user_config()`` (raises ChatwootConfigError when the
        agent/admin token or base URL is missing) with ``_user_headers()`` — the
        two are always called together at user-token endpoints.
        """
        self._check_user_config()
        return self._user_headers()

    def _url(self, suffix: str) -> str:
        return f"{self.base_url}/{suffix.lstrip('/')}"

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        """Exponential backoff with full jitter.

        Sequence for the default ``max_retries=2``:
          attempt 1 → between 0.0 and 0.5s
          attempt 2 → between 0.0 and 1.0s

        Full jitter avoids the thundering-herd retry-storm pattern when
        Chatwoot is recovering from a brief outage and every Polaris
        instance retries on the same boundary.
        """
        base = 0.5 * (2 ** (attempt - 1))
        return random.uniform(0.0, base)  # noqa: S311 — non-crypto: HTTP backoff jitter

    async def _request_with_retry(
        self,
        *,
        method: str,
        url: str,
        json_body: dict[str, Any] | None,
        op: str,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Issue an HTTP request with retry on transient failures.

        Returns the ``httpx.Response`` on 2xx OR on any 4xx (so the
        caller can parse the structured error envelope and decide).
        Raises ``ChatwootTransientError`` when the retry budget is
        consumed by 5xx / timeouts / connection-resets.

        ``op`` is a short opcode string ("flip_status", "ensure_label",
        ...) for logging — keeps log lines greppable without leaking
        request payloads to ops logs.

        ``headers`` defaults to ``_headers()`` (bot token). Pass
        ``_user_headers()`` for endpoints that require a user token.
        """
        last_exc: BaseException | None = None
        # max_retries=2 means: 1 initial attempt + 2 retries = 3 total tries.
        total_attempts = self.max_retries + 1
        _headers = headers if headers is not None else self._headers()
        for attempt in range(1, total_attempts + 1):
            try:
                # follow_redirects=True: a Chatwoot instance with FORCE_SSL (or
                # behind a proxy that 301s http→https) would otherwise return a
                # 3xx with an HTML body, which slips past the <400 check and then
                # fails JSON parsing as "response is not valid JSON". audit Batch
                # B (B6): shared-or-fresh client; timeout + follow_redirects are
                # passed per-request so both are honored on either path.
                async with _get_client(self.timeout_s) as client:
                    response = await client.request(
                        method,
                        url,
                        json=json_body,
                        headers=_headers,
                        timeout=self.timeout_s,
                        follow_redirects=True,
                    )
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                logger.warning(
                    "chatwoot %s network failure: attempt=%d/%d err=%s",
                    op,
                    attempt,
                    total_attempts,
                    type(exc).__name__,
                )
                if attempt < total_attempts:
                    await asyncio.sleep(self._backoff_seconds(attempt))
                continue

            if response.status_code >= 500:
                logger.warning(
                    "chatwoot %s 5xx: attempt=%d/%d status=%d",
                    op,
                    attempt,
                    total_attempts,
                    response.status_code,
                )
                if attempt < total_attempts:
                    await asyncio.sleep(self._backoff_seconds(attempt))
                continue

            # 2xx or 4xx — return for caller to interpret.
            return response

        # Exhausted retry budget. Surface the most recent failure mode.
        if last_exc is not None:
            raise ChatwootTransientError(
                f"Chatwoot {op}: network failure after "
                f"{total_attempts} attempts ({type(last_exc).__name__})"
            ) from last_exc
        raise ChatwootTransientError(
            f"Chatwoot {op}: 5xx after {total_attempts} attempts"
        )

    def _check_4xx(self, response: httpx.Response, *, op: str) -> None:
        """Raise ChatwootStructuralError on 4xx responses.

        2xx responses fall through silently — caller handles the body.
        """
        if response.status_code < 400:
            return
        message = _parse_chatwoot_error(
            response, default=f"Chatwoot {op} failed: HTTP {response.status_code}"
        )
        body_text: str | None = None
        try:
            body_text = response.text[:500]
        except Exception:  # noqa: BLE001
            body_text = None
        logger.warning(
            "chatwoot %s 4xx: status=%d message=%r",
            op,
            response.status_code,
            message,
        )
        raise ChatwootStructuralError(
            message, status_code=response.status_code, response_body=body_text
        )

    def _conversation_url(self, *, account_id: int, conversation_id: int) -> str:
        return self._url(
            f"api/v1/accounts/{account_id}/conversations/{conversation_id}"
        )

    # -----------------------------------------------------------------
    # Public verbs (6)
    # -----------------------------------------------------------------

    async def flip_conversation_status(
        self,
        *,
        account_id: int,
        conversation_id: int,
        status: str = "open",
    ) -> None:
        """POST /toggle_status — flip a Chatwoot conversation status.

        Phase 7 only writes ``"open"`` (used at the end of a handoff
        orchestration to route the conversation into the agent queue).
        Other values exist in the Chatwoot wire vocabulary but are not
        called from Polaris at GA.
        """
        self._check_config()
        url = (
            self._conversation_url(
                account_id=account_id, conversation_id=conversation_id
            )
            + "/toggle_status"
        )
        response = await self._request_with_retry(
            method="POST",
            url=url,
            json_body={"status": status},
            op="flip_status",
        )
        self._check_4xx(response, op="flip_status")

    async def assign_team(
        self, *, account_id: int, conversation_id: int, team_id: int
    ) -> None:
        """POST /assignments — assign a conversation to a Chatwoot team.

        ``team_id`` is the integer team id from the target Chatwoot
        account. Flow definitions carry HandoffNode.team_id as a string
        per Phase 5 schema; the caller (handoff service) does the
        int conversion + maps the flow string to the per-tenant team id
        before invoking here.
        """
        self._check_config()
        url = (
            self._conversation_url(
                account_id=account_id, conversation_id=conversation_id
            )
            + "/assignments"
        )
        response = await self._request_with_retry(
            method="POST",
            url=url,
            json_body={"team_id": team_id},
            op="assign_team",
        )
        self._check_4xx(response, op="assign_team")

    async def post_private_note(
        self, *, account_id: int, conversation_id: int, content: str
    ) -> int:
        """POST /messages with ``private=True``. Returns the message id.

        Posts a private note (invisible to the customer; only the agent
        sees it in their Chatwoot UI). Caller (handoff service) renders
        the configured private-note template with substituted variables;
        truncation to ``settings.handoff_private_note_max_len`` happens
        upstream of this call.
        """
        self._check_config()
        url = (
            self._conversation_url(
                account_id=account_id, conversation_id=conversation_id
            )
            + "/messages"
        )
        response = await self._request_with_retry(
            method="POST",
            url=url,
            json_body={
                "content": content,
                "private": True,
                "message_type": "outgoing",
            },
            op="private_note",
        )
        self._check_4xx(response, op="private_note")
        data = response.json()
        msg_id = data.get("id")
        if not isinstance(msg_id, int):
            raise ChatwootStructuralError(
                "private_note response had no integer .id",
                status_code=response.status_code,
            )
        return msg_id

    async def apply_conversation_label(
        self,
        *,
        account_id: int,
        conversation_id: int,
        label: str,
    ) -> None:
        """POST /conversations/{id}/labels — apply a label to a conversation.

        Used for ``pol_bot_suspended`` (PR-4) and any tenant-defined
        labels surfaced by future flow nodes. Idempotent server-side —
        re-applying an existing label is a 200 no-op.

        When ``CHATWOOT_LABEL_AUTO_PROVISION=true``, the client calls
        ``ensure_label`` first so a label that has never existed in the
        tenant's account gets created. Phase 9 platform-admin tenant
        provisioning also seeds the GA labels; this is defence-in-depth.

        Requires user token — bot tokens are rejected by CW for label endpoints.
        """
        uh = self._user_checked_headers()
        if self.label_auto_provision:
            await self.ensure_label(account_id=account_id, label=label)
        url = (
            self._conversation_url(
                account_id=account_id, conversation_id=conversation_id
            )
            + "/labels"
        )
        response = await self._request_with_retry(
            method="POST",
            url=url,
            json_body={"labels": [label]},
            op="apply_conversation_label",
            headers=uh,
        )
        self._check_4xx(response, op="apply_conversation_label")

    async def get_conversation_labels(
        self, *, account_id: int, conversation_id: int
    ) -> list[str]:
        """GET /conversations/{id}/labels — return the current label titles.

        Used by the handling-label sync (``label_sync.sync_handling_label``)
        to read-modify-write: Chatwoot's conversation-labels endpoint is
        set-style (replace, not append), so applying ``bot-handling`` /
        ``human-handling`` without first reading the set would clobber
        unrelated labels (e.g. ``pol_bot_suspended``).

        Returns ``[]`` when the payload is absent or malformed. Requires the
        user token — bot tokens are rejected by CW for label endpoints.
        """
        url = (
            self._conversation_url(
                account_id=account_id, conversation_id=conversation_id
            )
            + "/labels"
        )
        response = await self._request_with_retry(
            method="GET",
            url=url,
            json_body=None,
            op="get_conversation_labels",
            headers=self._user_checked_headers(),
        )
        self._check_4xx(response, op="get_conversation_labels")
        try:
            payload = response.json()
        except ValueError:
            logger.warning(
                "get_conversation_labels: non-JSON 200 from Chatwoot "
                "(account=%s conv=%s) — treating as empty label set",
                account_id,
                conversation_id,
            )
            return []
        labels = payload.get("payload") if isinstance(payload, dict) else None
        if not isinstance(labels, list):
            return []
        return [entry for entry in labels if isinstance(entry, str)]

    async def set_conversation_labels(
        self, *, account_id: int, conversation_id: int, labels: list[str]
    ) -> None:
        """POST /conversations/{id}/labels — set the FULL label set (replace).

        Chatwoot replaces the conversation's labels with exactly ``labels``.
        Callers that want to add/remove a single label without clobbering
        the rest must read (``get_conversation_labels``) → merge → set; that
        merge lives in ``label_sync.compute_handling_labels``.

        Requires the user token.
        """
        url = (
            self._conversation_url(
                account_id=account_id, conversation_id=conversation_id
            )
            + "/labels"
        )
        response = await self._request_with_retry(
            method="POST",
            url=url,
            json_body={"labels": labels},
            op="set_conversation_labels",
            headers=self._user_checked_headers(),
        )
        self._check_4xx(response, op="set_conversation_labels")

    async def apply_contact_label(
        self, *, account_id: int, contact_id: int, label: str
    ) -> None:
        """POST /contacts/{id}/labels — apply a label to a contact.

        Used for ``pol_opted_out`` (PR-4 label_sync) where the opt-out
        applies to the person, not the conversation. See ChatwootLabelTarget
        in shared types. Requires user token — bot tokens are rejected.
        """
        uh = self._user_checked_headers()
        if self.label_auto_provision:
            await self.ensure_label(account_id=account_id, label=label)
        url = self._url(
            f"api/v1/accounts/{account_id}/contacts/{contact_id}/labels"
        )
        response = await self._request_with_retry(
            method="POST",
            url=url,
            json_body={"labels": [label]},
            op="apply_contact_label",
            headers=uh,
        )
        self._check_4xx(response, op="apply_contact_label")

    async def ensure_label(self, *, account_id: int, label: str) -> bool:
        """GET → POST /labels — idempotent label-create.

        Returns ``True`` if the label was created in this call, ``False``
        if it already existed (or auto-provision is disabled).

        Implementation:
          1. GET /labels  → list existing labels for the account
          2. If ``label`` already present → return False
          3. POST /labels with ``{title, color, show_on_sidebar}``
             - On 422 "Title has already been taken" (race with another
               concurrent ensure_label call): treat as success (the
               label is now guaranteed to exist) → return False.
             - Other 4xx → ChatwootStructuralError.

        ``CHATWOOT_LABEL_AUTO_PROVISION=false`` short-circuits to a
        no-op (returns False) — used in tests + environments where
        labels are pre-seeded out-of-band.
        """
        self._check_user_config()
        if not self.label_auto_provision:
            return False
        uh = self._user_headers()
        list_url = self._url(f"api/v1/accounts/{account_id}/labels")
        list_resp = await self._request_with_retry(
            method="GET", url=list_url, json_body=None, op="list_labels", headers=uh
        )
        self._check_4xx(list_resp, op="list_labels")
        try:
            payload = list_resp.json()
        except ValueError:
            payload = {}
        labels = payload.get("payload") if isinstance(payload, dict) else None
        if isinstance(labels, list):
            for entry in labels:
                if isinstance(entry, dict) and entry.get("title") == label:
                    return False
        # Not present — create.
        create_resp = await self._request_with_retry(
            method="POST",
            url=list_url,
            json_body={
                "title": label,
                "color": _DEFAULT_LABEL_COLOR,
                "show_on_sidebar": True,
            },
            op="create_label",
            headers=uh,
        )
        if create_resp.status_code == 422:
            # 422 is ambiguous: it MIGHT be the race with a concurrent
            # ensure_label call (Chatwoot returns 422 "Title has already
            # been taken"), OR a real schema-validation failure (e.g. label
            # title violates a regex). Sniff the body before swallowing —
            # a real validation failure should NOT be silently treated as
            # success (per security review N-3).
            race_msg = _parse_chatwoot_error(create_resp, default="")
            if "already been taken" in race_msg.lower():
                logger.info(
                    "chatwoot ensure_label 422 (race with concurrent create): "
                    "account_id=%d label=%s",
                    account_id,
                    label,
                )
                return False
            logger.warning(
                "chatwoot ensure_label 422 NOT a race (likely schema "
                "validation): account_id=%d label=%s message=%r",
                account_id,
                label,
                race_msg,
            )
            # Fall through to _check_4xx which will raise ChatwootStructuralError.
        self._check_4xx(create_resp, op="create_label")
        return True

    async def ensure_contact(
        self,
        *,
        account_id: int,
        name: str | None = None,
        phone_number: str | None = None,
        identifier: str | None = None,
    ) -> int:
        """GET /contacts/search → POST /contacts — find-or-create a contact.

        Returns the CW contact id. Exactly one of ``phone_number`` /
        ``identifier`` must be supplied (raises ``ValueError`` otherwise):

          * **phone_number** (WhatsApp): the contact is keyed on its E.164
            phone number. ``phone_number`` is normalised to E.164 (``+``
            prepended if absent) before searching and creating.
          * **identifier** (Instagram / Messenger / Telegram): these channels
            have no phone number — Chatwoot rejects (HTTP 422) a contact
            created with a non-phone value in ``phone_number``. Instead we use
            Chatwoot's per-account-unique ``identifier`` field (e.g.
            ``"instagram:<psid>"``).

        Search-first pattern: Chatwoot's contact-create endpoint does NOT
        de-duplicate — calling POST twice for the same key creates two
        contacts. We look up first (GET /contacts/search for the phone path,
        POST /contacts/filter exact-match for the identifier path) and only
        POST /contacts when the key is genuinely absent. For the identifier
        path a unique-constraint 422 on create is treated as a lost race: we
        re-search and return the existing id rather than crashing.

        Currently unused by the inbound pipeline: under Chatwoot-native ingress
        (Meta + Telegram + email) Chatwoot owns contact/conversation creation, so
        Polaris only LINKS the ids the agent-bot webhook carries
        (``pipeline._link_cw_conversation``) rather than creating them. This
        find-or-create helper was the old ``_ensure_cw_conversation`` mirror path
        (deleted in the Chatwoot-native rehaul, PR #102); it is kept as a client
        capability for any future own-webhook channel that must mirror a contact
        into Chatwoot itself.
        """
        # Exactly one of phone_number / identifier must be TRUTHY — reject
        # both-set, neither-set, AND empty strings (an empty identifier would
        # otherwise create a junk ``identifier=""`` contact, and an empty
        # phone_number would 422).
        if bool(phone_number) == bool(identifier):
            raise ValueError(
                "ensure_contact requires exactly one of phone_number / identifier"
            )
        uh = self._user_checked_headers()

        if phone_number:
            return await self._ensure_contact_by_phone(
                account_id=account_id, phone_number=phone_number, name=name, uh=uh
            )
        assert identifier  # narrowed by the truthy XOR guard above
        return await self._ensure_contact_by_identifier(
            account_id=account_id, identifier=identifier, name=name, uh=uh
        )

    @staticmethod
    def _extract_contact_id(body: Any) -> int | None:
        """Pull the contact id out of a create/search payload.

        CW returns ``{"payload": {"contact": {"id": ...}, "contact_inbox":
        {...}}}`` on create; fall back to a top-level ``id`` for
        forward-compat.
        """
        contact_id: Any = None
        if isinstance(body, dict):
            payload = body.get("payload")
            if isinstance(payload, dict):
                contact_obj = payload.get("contact")
                if isinstance(contact_obj, dict):
                    contact_id = contact_obj.get("id")
            if not isinstance(contact_id, int):
                contact_id = body.get("id")
        return contact_id if isinstance(contact_id, int) else None

    async def _ensure_contact_by_phone(
        self,
        *,
        account_id: int,
        phone_number: str,
        name: str | None,
        uh: dict[str, str],
    ) -> int:
        """WhatsApp path — find-or-create keyed on E.164 phone number."""
        e164 = phone_number if phone_number.startswith("+") else f"+{phone_number}"

        # 1. Search existing contacts.
        search_url = (
            self._url(f"api/v1/accounts/{account_id}/contacts/search")
            + f"?q={quote(e164, safe='')}&page=1"
        )
        search_resp = await self._request_with_retry(
            method="GET", url=search_url, json_body=None, op="search_contact", headers=uh
        )
        self._check_4xx(search_resp, op="search_contact")
        try:
            search_body = search_resp.json()
        except ValueError:
            search_body = {}
        contacts = search_body.get("payload") if isinstance(search_body, dict) else []
        if isinstance(contacts, list):
            for c in contacts:
                if isinstance(c, dict) and c.get("phone_number") in (
                    phone_number,
                    e164,
                ):
                    cid = c.get("id")
                    if isinstance(cid, int):
                        return cid

        # 2. Not found — create.
        create_payload: dict[str, Any] = {"phone_number": e164}
        if name:
            create_payload["name"] = name
        create_resp = await self._request_with_retry(
            method="POST",
            url=self._url(f"api/v1/accounts/{account_id}/contacts"),
            json_body=create_payload,
            op="create_contact",
            headers=uh,
        )
        self._check_4xx(create_resp, op="create_contact")
        try:
            create_body = create_resp.json()
        except ValueError:
            create_body = {}
        contact_id = self._extract_contact_id(create_body)
        if contact_id is None:
            raise ChatwootStructuralError(
                "create_contact: response has no integer .id",
                status_code=create_resp.status_code,
            )
        return contact_id

    async def _search_contact_by_identifier(
        self,
        *,
        account_id: int,
        identifier: str,
        uh: dict[str, str],
    ) -> int | None:
        """POST /contacts/filter → return the id of a contact whose
        ``identifier`` matches exactly, or ``None`` when absent.

        Uses the fork's ``Contacts::FilterService`` (POST /contacts/filter)
        with an ``equal_to`` operator on the ``identifier`` attribute rather
        than the fuzzy GET /contacts/search (which ILIKEs a ``%q%`` substring,
        returns only page 1, and would silently miss a contact on a large
        account when the exact match falls past the first page of partial
        matches). We still exact-check ``identifier`` defensively on the
        returned payload.
        """
        filter_url = self._url(f"api/v1/accounts/{account_id}/contacts/filter")
        filter_body: dict[str, Any] = {
            "payload": [
                {
                    "attribute_key": "identifier",
                    "filter_operator": "equal_to",
                    "values": [identifier],
                    "query_operator": None,
                }
            ]
        }
        filter_resp = await self._request_with_retry(
            method="POST",
            url=filter_url,
            json_body=filter_body,
            op="filter_contact",
            headers=uh,
        )
        self._check_4xx(filter_resp, op="filter_contact")
        try:
            filter_result = filter_resp.json()
        except ValueError:
            filter_result = {}
        contacts = (
            filter_result.get("payload") if isinstance(filter_result, dict) else []
        )
        if isinstance(contacts, list):
            for c in contacts:
                if isinstance(c, dict) and c.get("identifier") == identifier:
                    cid = c.get("id")
                    if isinstance(cid, int):
                        return cid
        return None

    async def _ensure_contact_by_identifier(
        self,
        *,
        account_id: int,
        identifier: str,
        name: str | None,
        uh: dict[str, str],
    ) -> int:
        """IG / Messenger / Telegram path — find-or-create keyed on the
        per-account-unique ``identifier`` (no phone_number)."""
        found = await self._search_contact_by_identifier(
            account_id=account_id, identifier=identifier, uh=uh
        )
        if found is not None:
            return found

        create_payload: dict[str, Any] = {"identifier": identifier}
        if name:
            create_payload["name"] = name
        create_resp = await self._request_with_retry(
            method="POST",
            url=self._url(f"api/v1/accounts/{account_id}/contacts"),
            json_body=create_payload,
            op="create_contact",
            headers=uh,
        )
        if create_resp.status_code == 422:
            # 422 is ambiguous (mirrors ensure_label): it MIGHT be the
            # unique-identifier race — a concurrent inbound created the same
            # contact between our filter and this POST (Chatwoot returns 422
            # "Identifier has already been taken") — OR a real validation
            # failure. Sniff the body before re-searching: only the race is
            # safe to swallow.
            race_msg = _parse_chatwoot_error(create_resp, default="")
            if "already been taken" in race_msg.lower():
                logger.info(
                    "chatwoot ensure_contact 422 (identifier race): account_id=%d",
                    account_id,
                )
                raced = await self._search_contact_by_identifier(
                    account_id=account_id, identifier=identifier, uh=uh
                )
                if raced is not None:
                    return raced
            else:
                logger.warning(
                    "chatwoot ensure_contact 422 NOT a race (likely validation): "
                    "account_id=%d message=%r",
                    account_id,
                    race_msg,
                )
            # Fall through to _check_4xx which raises ChatwootStructuralError.
        self._check_4xx(create_resp, op="create_contact")
        try:
            create_body = create_resp.json()
        except ValueError:
            create_body = {}
        contact_id = self._extract_contact_id(create_body)
        if contact_id is None:
            raise ChatwootStructuralError(
                "create_contact: response has no integer .id",
                status_code=create_resp.status_code,
            )
        return contact_id

    async def create_conversation(
        self,
        *,
        account_id: int,
        inbox_id: int,
        contact_id: int,
        source_id: str | None = None,
    ) -> int:
        """POST /conversations — create a new conversation in a CW inbox.

        Returns the CW conversation id. Does NOT de-duplicate — callers must
        check ``conversations.chatwoot_conversation_id IS NULL`` before
        calling to avoid creating orphaned CW conversations.

        ``source_id`` (when provided) is the provider-side user id (PSID /
        IGSID / Telegram chat id / WA phone). Chatwoot uses it to build the
        ``contact_inbox`` link so the conversation is correctly addressed to
        the customer on the channel.
        """
        url = self._url(f"api/v1/accounts/{account_id}/conversations")
        json_body: dict[str, Any] = {"inbox_id": inbox_id, "contact_id": contact_id}
        if source_id is not None:
            json_body["source_id"] = source_id
        response = await self._request_with_retry(
            method="POST",
            url=url,
            json_body=json_body,
            op="create_conversation",
            headers=self._user_checked_headers(),
        )
        self._check_4xx(response, op="create_conversation")
        try:
            body = response.json()
        except ValueError:
            body = {}
        conv_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(conv_id, int):
            raise ChatwootStructuralError(
                "create_conversation: response has no integer .id",
                status_code=response.status_code,
            )
        return conv_id

    async def get_inbox(self, *, account_id: int, inbox_id: int) -> dict[str, Any]:
        """GET /api/v1/accounts/{account_id}/inboxes/{inbox_id} — fetch inbox details.

        Returns the raw Chatwoot inbox object. Used by the admin channel add
        modal to auto-fill platform-specific IDs (waba_id, page_id, etc.).
        Raises ``ChatwootStructuralError`` on 4xx (e.g. inbox not found).
        """
        url = self._url(f"api/v1/accounts/{account_id}/inboxes/{inbox_id}")
        response = await self._request_with_retry(
            method="GET",
            url=url,
            json_body=None,
            op="get_inbox",
            headers=self._user_checked_headers(),
        )
        self._check_4xx(response, op="get_inbox")
        try:
            data: dict[str, Any] = response.json()
            return data
        except ValueError as exc:
            raise ChatwootStructuralError(
                "get_inbox: response is not valid JSON",
                status_code=response.status_code,
            ) from exc

    async def get_contact_details(
        self, *, account_id: int, conversation_id: int
    ) -> dict[str, str]:
        """Return ``{name, email, phone}`` for a conversation's contact.

        Reads the conversation's ``meta.sender`` (Chatwoot's contact projection)
        and returns only the non-empty string fields, normalised to the keys
        ``name`` / ``email`` / ``phone`` (CW exposes the phone as
        ``phone_number``). Used to **prefill** the in-chat booking details capture
        so we only ask the customer for what Chatwoot doesn't already know.

        Best-effort: any error (HTTP failure, 4xx, malformed body, missing
        sender) returns ``{}`` so a Chatwoot hiccup never blocks the booking — the
        caller just asks for the missing fields.
        """
        url = self._url(
            f"api/v1/accounts/{account_id}/conversations/{conversation_id}"
        )
        try:
            response = await self._request_with_retry(
                method="GET",
                url=url,
                json_body=None,
                op="get_contact_details",
                headers=self._user_checked_headers(),
            )
            self._check_4xx(response, op="get_contact_details")
            body = response.json()
        except Exception:  # noqa: BLE001 — prefill is best-effort; never raise
            logger.warning(
                "get_contact_details failed account=%s conv=%s — no prefill",
                account_id,
                conversation_id,
            )
            return {}
        meta = body.get("meta") if isinstance(body, dict) else None
        sender = meta.get("sender") if isinstance(meta, dict) else None
        if not isinstance(sender, dict):
            return {}
        out: dict[str, str] = {}
        name = sender.get("name")
        if isinstance(name, str) and name.strip():
            out["name"] = name.strip()
        email = sender.get("email")
        if isinstance(email, str) and email.strip():
            out["email"] = email.strip()
        phone = sender.get("phone_number")
        if isinstance(phone, str) and phone.strip():
            out["phone"] = phone.strip()
        return out

    def _parse_list_payload(
        self, response: httpx.Response, op: str
    ) -> list[dict[str, Any]]:
        """Unwrap + validate a Chatwoot list-index response body.

        CW renders index endpoints either as a bare array or as a
        ``{"payload": [...]}`` wrap — tolerate both. Raises
        ``ChatwootStructuralError`` on non-JSON or a non-list payload.
        """
        try:
            body = response.json()
        except ValueError as exc:
            raise ChatwootStructuralError(
                f"{op}: response is not valid JSON",
                status_code=response.status_code,
            ) from exc
        payload = body.get("payload") if isinstance(body, dict) else body
        if not isinstance(payload, list):
            raise ChatwootStructuralError(
                f"{op}: expected list payload, got {type(payload).__name__}",
                status_code=response.status_code,
            )
        return payload

    async def list_inboxes(self, *, account_id: int) -> list[dict[str, Any]]:
        """GET /api/v1/accounts/{account_id}/inboxes — list all inboxes.

        Returns the payload array. Used to discover email inboxes for a tenant
        (email channels have no tenant_channels row in Polaris — they're
        Chatwoot-native).
        """
        url = self._url(f"api/v1/accounts/{account_id}/inboxes")
        response = await self._request_with_retry(
            method="GET",
            url=url,
            json_body=None,
            op="list_inboxes",
            headers=self._user_checked_headers(),
        )
        self._check_4xx(response, op="list_inboxes")
        return self._parse_list_payload(response, "list_inboxes")

    async def update_inbox_channel(
        self,
        *,
        account_id: int,
        inbox_id: int,
        channel_attrs: dict[str, Any],
    ) -> None:
        """PATCH /api/v1/accounts/{account_id}/inboxes/{inbox_id}.

        Merges ``channel_attrs`` into the inbox's channel attributes. Used to
        rotate IMAP/SMTP passwords for email inboxes — credentials live in
        Chatwoot directly, not in Polaris tenant_secrets.
        Raises ``ChatwootStructuralError`` on 4xx.
        """
        url = self._url(f"api/v1/accounts/{account_id}/inboxes/{inbox_id}")
        response = await self._request_with_retry(
            method="PATCH",
            url=url,
            json_body={"channel": channel_attrs},
            op="update_inbox_channel",
            headers=self._user_checked_headers(),
        )
        self._check_4xx(response, op="update_inbox_channel")

    async def list_agent_bots(self, *, account_id: int) -> list[dict[str, Any]]:
        """GET /api/v1/accounts/{account_id}/agent_bots — list bots for the account.

        Chatwoot's ``AgentBot.accessible_to(account)`` scope includes BOTH the
        account's own bots AND platform-level bots (``account_id IS NULL``), so
        this surfaces the shared 'Polaris Bot' the auto-attach resolves by name.
        Requires a user/admin token.
        """
        url = self._url(f"api/v1/accounts/{account_id}/agent_bots")
        response = await self._request_with_retry(
            method="GET",
            url=url,
            json_body=None,
            op="list_agent_bots",
            headers=self._user_checked_headers(),
        )
        self._check_4xx(response, op="list_agent_bots")
        return self._parse_list_payload(response, "list_agent_bots")

    async def create_agent_bot(
        self, *, account_id: int, name: str, outgoing_url: str
    ) -> int:
        """POST /api/v1/accounts/{account_id}/agent_bots — create a webhook bot.

        Returns the new bot id. Used only for fresh-account bootstrap; the
        shared platform bot is normally seeded out-of-band and resolved via
        ``list_agent_bots``. Caller owns idempotency (check the list first).
        Requires a user/admin token.
        """
        url = self._url(f"api/v1/accounts/{account_id}/agent_bots")
        response = await self._request_with_retry(
            method="POST",
            url=url,
            json_body={"name": name, "outgoing_url": outgoing_url},
            op="create_agent_bot",
            headers=self._user_checked_headers(),
        )
        self._check_4xx(response, op="create_agent_bot")
        try:
            body = response.json()
        except ValueError as exc:
            raise ChatwootStructuralError(
                "create_agent_bot: response is not valid JSON",
                status_code=response.status_code,
            ) from exc
        bot_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(bot_id, int):
            raise ChatwootStructuralError(
                "create_agent_bot: response has no integer .id",
                status_code=response.status_code,
            )
        return bot_id

    async def set_inbox_agent_bot(
        self, *, account_id: int, inbox_id: int, agent_bot_id: int | None
    ) -> None:
        """POST /api/v1/accounts/{account_id}/inboxes/{inbox_id}/set_agent_bot.

        Attaches ``agent_bot_id`` to the inbox so new conversations start in
        ``pending`` (bot-owned) — which is what the human-handling gate + native
        escalation depend on. Pass ``None`` to detach. Idempotent server-side
        (find_or_create of the AgentBotInbox link); returns 200 with an empty
        body. Requires a user/admin token.
        """
        url = self._url(
            f"api/v1/accounts/{account_id}/inboxes/{inbox_id}/set_agent_bot"
        )
        response = await self._request_with_retry(
            method="POST",
            url=url,
            json_body={"agent_bot": agent_bot_id},
            op="set_agent_bot",
            headers=self._user_checked_headers(),
        )
        self._check_4xx(response, op="set_agent_bot")

    async def post_outbound_message(
        self,
        *,
        account_id: int,
        conversation_id: int,
        content: str,
        source_id: str | None = None,
    ) -> None:
        """POST /messages — post a bot reply as an outbound (visible) message.

        Used to mirror the bot's reply into the CW conversation so agents
        see the conversation history and ``pollCwBotReply`` in E2E specs can
        verify the bot responded.  ``message_type: "outgoing"`` maps to
        CW's ``message_type=1`` (outbound) in the REST envelope.

        ``source_id`` should be set to the provider message-id already
        returned by the Meta / Telegram send (e.g. the WhatsApp ``wamid``
        or Messenger ``mid``).  When present, Chatwoot's
        ``base/send_on_channel_service.rb`` sees ``message.source_id.present?``
        as true and skips re-sending the message on the channel — preventing
        the customer from receiving the reply a second time.  Pass ``None``
        only when no provider id is available (e.g. the upstream send failed).
        """
        self._check_config()
        url = (
            self._conversation_url(
                account_id=account_id, conversation_id=conversation_id
            )
            + "/messages"
        )
        json_body: dict[str, object] = {
            "content": content,
            "private": False,
            "message_type": "outgoing",
        }
        if source_id is not None:
            json_body["source_id"] = source_id
        response = await self._request_with_retry(
            method="POST",
            url=url,
            json_body=json_body,
            op="post_outbound_message",
        )
        self._check_4xx(response, op="post_outbound_message")

    async def get_channel_token(
        self,
        *,
        account_id: int,
        inbox_id: int,
    ) -> str | None:
        """GET /internal/inboxes/{inbox_id}/token — fetch the channel access token.

        Uses the Polaris-internal endpoint on the Chatwoot Rails container (via
        ``chatwoot_internal_base_url`` — NOT the public Caddy base URL). The
        request is authenticated with ``X-Polaris-Internal-Token``.

        Returns the token string on 200; returns ``None`` on any of:
          - Internal token or internal base URL not configured.
          - HTTP 401/404/422/other non-200 response.
          - JSON parse failure or missing ``token`` key in the body.
          - Network timeout or connection error.

        This method is **best-effort**: it never raises into its caller. All
        failure modes are logged as WARNING. The token value itself is NEVER
        logged.

        ``account_id`` is accepted to keep a consistent call-site signature
        with other client methods even though the internal endpoint doesn't
        require it in the path — reserved for future per-account routing.
        """
        settings = get_settings()
        internal_token = settings.chatwoot_internal_token
        internal_base = settings.chatwoot_internal_base_url.rstrip("/")

        if not internal_token or not internal_base:
            logger.warning(
                "get_channel_token: CHATWOOT_INTERNAL_TOKEN or "
                "CHATWOOT_INTERNAL_BASE_URL not configured — skipping"
            )
            return None

        url = f"{internal_base}/internal/inboxes/{inbox_id}/token?account_id={account_id}"
        headers = {
            "X-Polaris-Internal-Token": internal_token,
            "Content-Type": "application/json",
        }

        try:
            async with _get_client(self.timeout_s) as client:
                response = await client.get(
                    url, headers=headers, timeout=self.timeout_s,
                    follow_redirects=False,
                )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning(
                "get_channel_token: network failure for inbox=%d err=%s",
                inbox_id,
                type(exc).__name__,
            )
            return None

        if response.status_code != 200:
            logger.warning(
                "get_channel_token: unexpected status inbox=%d status=%d",
                inbox_id,
                response.status_code,
            )
            return None

        try:
            body = response.json()
        except ValueError:
            logger.warning(
                "get_channel_token: response not valid JSON inbox=%d",
                inbox_id,
            )
            return None

        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            logger.warning(
                "get_channel_token: missing or invalid token field inbox=%d",
                inbox_id,
            )
            return None

        # Never log the token value itself.
        return token


# =====================================================================
# Factory
# =====================================================================


@lru_cache(maxsize=1)
def get_chatwoot_client() -> ChatwootClient:
    """Lazy singleton — constructed from env on first call.

    Tests monkeypatch via the factory after ``reset_client_for_testing()``.
    """
    settings = get_settings()
    return ChatwootClient(
        base_url=settings.chatwoot_base_url,
        bot_access_token=settings.chatwoot_bot_access_token,
        agent_api_token=settings.chatwoot_agent_api_token,
        timeout_s=settings.chatwoot_api_timeout_s,
        max_retries=settings.chatwoot_api_max_retries,
        label_auto_provision=settings.chatwoot_label_auto_provision,
    )


def reset_client_for_testing() -> None:
    """Clear the lru_cache so tests can monkeypatch a fresh factory."""
    clear = getattr(get_chatwoot_client, "cache_clear", None)
    if callable(clear):
        clear()


