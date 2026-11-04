"""Easy!Appointments fork (polaris-ea-fork) REST client — Phase 7 PR-6.

Bearer-JWT client against the EA fork image
``ghcr.io/jiun01/polaris-ea:v1.6.0-r2``. The EA fork ships a multi-
tenant CodeIgniter 3 + MySQL backend per ADR-024 + ADR-013 Amendment;
tenant scoping is enforced server-side via the JWT's ``tenant_id``
claim against ``MY_Model``'s tenant-context setter.

URL pattern: ``{base_url}/index.php/api/v1/{resource}`` (CodeIgniter 3
convention — the ``index.php`` segment is the front controller).

Authentication: per-call HS256 JWT with claims
``{tenant_id, aud='ea-fork', iss='polaris', iat, exp}``. Signing secret
is a single platform-wide ``EA_JWT_SIGNING_SECRET`` env var — same
model as the Chatwoot bot token (Q-PHASE7-01): per-tenant scope flows
through the claim payload, not through per-tenant secrets storage.
Short TTL (~5 min default) bounds replay surface.

Error taxonomy mirrors ChatwootClient + MessengerProfileClient:
  - ``EABookingTransientError``  — 5xx / timeout / connection
  - ``EABookingStructuralError`` — 4xx (404 not found, 400 validation,
                                   401 invalid JWT, 403 wrong tenant)
  - ``EABookingConfigError``     — placeholder / empty secret, missing
                                   base_url (fail-closed before HTTP)

ADR-051: caller (booking flow node) maps these to
``confirmed`` / ``cancelled`` / ``error`` edge outcomes.

Doc references:
  - ../polaris-ea-fork/docs/INTEGRATION_CHECKLIST.md (contract)
  - ../docs/Polaris V2/EA_Whitelabel/HANDOFF.md §6 (E2E scenarios)
  - upstream EA: https://easyappointments.org / openapi.yml
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any, Final
from urllib.parse import urlencode
from uuid import UUID

import httpx
import jwt as pyjwt

from polaris_api import http_client
from polaris_api.config import get_settings

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def _get_client(fallback_timeout: float) -> AsyncIterator[httpx.AsyncClient]:
    """Shared-or-fresh httpx client (audit Batch B / B6).

    Prefers the lifespan-scoped singleton (prod) — every request through it
    still passes its own explicit ``timeout=``, so behavior is identical to
    a fresh per-call client. Falls back to a fresh
    ``httpx.AsyncClient(timeout=fallback_timeout)`` — the previous
    behaviour — when the singleton isn't initialised (unit tests /
    scripts). Never closes the shared client (only the lifespan does).
    """
    shared = http_client.get_http_client_or_none()
    if shared is not None:
        yield shared
    else:
        async with httpx.AsyncClient(timeout=fallback_timeout) as client:
            yield client


_CI3_FRONT_CONTROLLER: Final[str] = "index.php"

# EA's list endpoints page at ``default_length = 20`` (application/libraries/
# Api.php) when no ``length`` param is sent. A tenant has ~50 settings rows, so
# an unpaginated GET silently truncates — dropping later-sorted keys like
# ``default_timezone`` / ``future_booking_limit``. Request a length far above any
# realistic per-tenant row count so a single call returns everything.
_EA_FETCH_ALL_LENGTH: Final[int] = 1000


# =====================================================================
# Error taxonomy
# =====================================================================


class EABookingError(RuntimeError):
    """Base class. Carries ``status_code`` when applicable."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        ea_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        # Machine-readable code from EA's error envelope ({"error": "..."}) — e.g.
        # "username_taken" / "invalid_username" — surfaced to the operator UI.
        self.ea_code = ea_code


class EABookingConfigError(EABookingError):
    """JWT signing secret unset/placeholder, base_url missing.

    Fail-closed BEFORE any HTTP. Surfaced from the client construction
    boundary OR on first call when settings carry placeholder values.
    """


class EABookingTransientError(EABookingError):
    """5xx / timeout / connection error. Caller may classify as
    ``cancelled`` (EA temporarily unreachable; customer can retry via
    the public booking URL fallback)."""


class EABookingStructuralError(EABookingError):
    """4xx response. 401 = JWT invalid (rotate signing secret), 403 =
    wrong tenant claim (caller bug), 404 = resource missing, 400 =
    validation. Caller maps to ``error`` edge per ADR-051."""


def _parse_json_or_raise(response: httpx.Response, *, op: str) -> Any:
    """Decode a 2xx body or raise EABookingStructuralError with EA context.

    silent-failure-hunter SEV-2: CodeIgniter 3 can return 200 OK with an
    HTML error page (e.g. on a misconfigured route). The raw .json()
    call would raise ValueError, bypass our error taxonomy, and surface
    as a generic RUNTIME_PORT_ERROR at the Phase 5 flow-runner wrapper —
    no EA context preserved. Catching here keeps the failure inside the
    EA taxonomy.
    """
    try:
        return response.json()
    except ValueError as exc:
        body_preview: str
        try:
            body_preview = response.text[:200]
        except Exception:  # noqa: BLE001
            body_preview = "<unreadable>"
        logger.warning(
            "ea_booking %s 2xx-non-json: status=%d body=%r",
            op,
            response.status_code,
            body_preview,
        )
        raise EABookingStructuralError(
            f"EA {op} returned non-JSON body (preview={body_preview!r})",
            status_code=response.status_code,
        ) from exc


def _parse_ea_error_message(response: httpx.Response) -> str:
    """Extract ``message`` from EA's error envelope.

    EA returns ``{code: 404, message: "The requested record was not found!"}``
    on the API surface. Cap at 200 chars (defence-in-depth — same posture
    as messenger_profile._parse_meta_error_message).
    """
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        msg = body.get("message")
        if isinstance(msg, str) and msg:
            return msg[:200]
    return f"HTTP {response.status_code}"


def _parse_ea_error_code(response: httpx.Response) -> str | None:
    """Extract the machine code from EA's error envelope (``{"error": "..."}``).

    EA returns ``{error: "username_taken", message: "..."}`` on the internal
    provisioning surface. Returned so callers can map it to a friendly reason
    (vs the generic exception class name).
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        code = body.get("error")
        if isinstance(code, str) and code:
            return code[:64]
    return None


# =====================================================================
# Public URL composer
# =====================================================================


def public_booking_url(*, base_url: str, tenant_slug: str) -> str:
    """Compose the public self-service booking URL for a tenant.

    EA fork's slug-routed public booking page lives at
    ``{base_url}/book/{slug}`` (per ADR-013 Amendment 2026-05-16 + the
    EA fork's INTEGRATION_CHECKLIST §S3-4 slug controller patch).

    Used by the booking flow node's fallback path when interactive
    booking via the API fails — the bot replies with this URL so the
    customer can self-serve.

    Validation:
      - ``tenant_slug``: ASCII alphanumerics + dash + underscore only.
        The slug column in Polaris Postgres is UNIQUE + populated by
        Phase 1 tenant-provisioning code; this validator is
        defence-in-depth against any caller that might pass a raw
        user-provided string.
      - ``base_url``: must start with ``https://`` or ``http://``
        (security-reviewer Nit 2). Guards against a misconfigured
        ``EA_API_BASE_URL`` env producing a ``javascript:`` or relative
        URL that gets relayed to a customer.
    """
    if not tenant_slug:
        raise ValueError("tenant_slug must be non-empty")
    # Allowlist: ASCII letters, digits, hyphens, underscores.
    if not all(c.isalnum() or c in "-_" for c in tenant_slug):
        raise ValueError(
            f"tenant_slug {tenant_slug!r} contains invalid characters "
            "(allowed: ASCII alphanumerics + hyphen/underscore)"
        )
    if not base_url.startswith(("https://", "http://")):
        raise ValueError(
            f"base_url must use https:// or http://; got {base_url!r}"
        )
    return f"{base_url.rstrip('/')}/book/{tenant_slug}"


def build_ea_wizard_url(
    *,
    base_url: str,
    tenant_slug: str,
    service_id: int,
    provider_id: int,
    polaris_ref: str,
    channel: str | None = None,
    customer_handle: str | None = None,
) -> str:
    """Compose the full EA booking-wizard URL with the bound ``polaris_ref``.

    ``{base}/book/{slug}?service_id=&provider_id=&polaris_ref=[&prefill_phone]``.
    Shared by the ``/b/{jti}`` short-link redirect and the flow-runtime booking
    node, which previously assembled this query string inline in two places.
    WhatsApp handles add ``prefill_phone`` so EA pre-fills the customer's number;
    IG/Messenger ids are not phone numbers, so nothing is prefilled there.

    Raises ``ValueError`` (via :func:`public_booking_url`) on an invalid
    slug/base_url.
    """
    base = public_booking_url(base_url=base_url, tenant_slug=tenant_slug)
    params: dict[str, str | int] = {
        "service_id": service_id,
        "provider_id": provider_id,
        "polaris_ref": polaris_ref,
    }
    if channel == "whatsapp" and customer_handle:
        params["prefill_phone"] = customer_handle
    return f"{base}?{urlencode(params)}"


# =====================================================================
# Client
# =====================================================================


class EABookingClient:
    """Async REST client for the polaris-ea-fork API v1.

    Five public methods cover the bot-flow surface:
      - ``list_services`` — show the customer what they can book
      - ``list_availabilities`` — open slots for a service / provider / range
      - ``create_appointment`` — book a slot
      - ``cancel_appointment`` — cancel by id
      - ``reschedule_appointment`` — move start/end timestamps

    All five issue a fresh per-call JWT carrying the caller's
    ``tenant_id`` so the EA server enforces tenant scope. Polaris does
    NOT pass tenant_id explicitly in URLs — single source of truth is
    the JWT claim. (Defence-in-depth would add a query param too; the
    EA-side enforcement is what we trust per ADR-024.)
    """

    def __init__(
        self,
        *,
        base_url: str,
        jwt_signing_secret: str,
        jwt_audience: str,
        jwt_issuer: str,
        jwt_ttl_seconds: int,
        timeout_s: float,
        max_retries: int,
        provisioning_hmac_secret: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.jwt_signing_secret = jwt_signing_secret
        self.jwt_audience = jwt_audience
        self.jwt_issuer = jwt_issuer
        self.jwt_ttl_seconds = jwt_ttl_seconds
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        # HMAC secret for the internal provisioning endpoints (NOT the per-call
        # tenant JWT). Must equal the EA fork's POLARIS_WEBHOOK_SECRET.
        self.provisioning_hmac_secret = provisioning_hmac_secret

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _check_config(self) -> None:
        """Fail-closed on placeholder / empty config."""
        if not self.base_url:
            raise EABookingConfigError("EA_API_BASE_URL is unset.")
        if not self.jwt_signing_secret or self.jwt_signing_secret.startswith(
            "placeholder-"
        ):
            raise EABookingConfigError(
                "EA_JWT_SIGNING_SECRET is unset or placeholder. "
                "Generate a fresh 32-byte hex value, paste into .env, "
                "and mirror to the EA fork's config/polaris.php."
            )

    def _mint_jwt(self, tenant_id: UUID) -> str:
        """Sign a short-lived JWT carrying tenant context.

        Claims:
          - ``tenant_id`` (str, UUID-stringified) — primary scope key
          - ``aud`` = ``self.jwt_audience`` (default ``ea-fork``)
          - ``iss`` = ``self.jwt_issuer`` (default ``polaris``)
          - ``iat`` = now (epoch seconds)
          - ``exp`` = now + ``jwt_ttl_seconds``

        EA-side verifier (firebase/php-jwt in CodeIgniter 3, per
        INTEGRATION_CHECKLIST decision) checks aud + iss + exp + sig
        and rejects on any mismatch; then sets MY_Model's tenant
        context to the claim. Token reuse across tenants is impossible
        because the signature embeds the tenant_id claim.
        """
        now = datetime.now(UTC)
        payload: dict[str, Any] = {
            "tenant_id": str(tenant_id),
            "aud": self.jwt_audience,
            "iss": self.jwt_issuer,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=self.jwt_ttl_seconds)).timestamp()),
        }
        return pyjwt.encode(payload, self.jwt_signing_secret, algorithm="HS256")

    def _headers(self, tenant_id: UUID) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._mint_jwt(tenant_id)}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, suffix: str) -> str:
        return f"{self.base_url}/{_CI3_FRONT_CONTROLLER}/{suffix.lstrip('/')}"

    async def _request(
        self,
        *,
        method: str,
        url: str,
        tenant_id: UUID,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        op: str,
    ) -> httpx.Response:
        """Single request — no retry budget at GA.

        EA fork is a co-located service (same VPC at GA per the EA
        production cutover plan). Retries help with transient network
        blips but don't help with the dominant failure modes here
        (validation errors, 404s). The caller (booking flow node) is
        the right place to decide whether to retry — typically NO
        retry because the customer is in an interactive bot session
        and any extra latency degrades UX.
        """
        try:
            async with _get_client(self.timeout_s) as client:
                return await client.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    headers=self._headers(tenant_id),
                    timeout=self.timeout_s,
                )
        except httpx.TimeoutException as exc:
            raise EABookingTransientError(
                f"EA {op} timed out: {exc}"
            ) from exc
        except httpx.ConnectError as exc:
            raise EABookingTransientError(
                f"EA {op} connect error: {exc}"
            ) from exc

    def _raise_for_status(self, response: httpx.Response, *, op: str) -> None:
        if response.status_code < 400:
            return
        message = _parse_ea_error_message(response)
        if response.status_code >= 500:
            logger.warning(
                "ea_booking %s 5xx: status=%d msg=%r",
                op,
                response.status_code,
                message,
            )
            raise EABookingTransientError(
                message, status_code=response.status_code
            )
        # 4xx — never log the JWT (Authorization header isn't logged anyway)
        logger.warning(
            "ea_booking %s 4xx: status=%d msg=%r",
            op,
            response.status_code,
            message,
        )
        raise EABookingStructuralError(
            message,
            status_code=response.status_code,
            ea_code=_parse_ea_error_code(response),
        )

    # -----------------------------------------------------------------
    # Public verbs
    # -----------------------------------------------------------------

    async def list_services(
        self, *, tenant_id: UUID
    ) -> list[dict[str, Any]]:
        """GET /api/v1/services — list services available to the tenant.

        Returns the JSON array as-is from EA. Per ADR-024 the EA
        backend filters to ``tenant_id`` matching the JWT claim, so
        the response only contains the calling tenant's services.
        """
        self._check_config()
        response = await self._request(
            method="GET",
            url=self._url("api/v1/services"),
            tenant_id=tenant_id,
            op="list_services",
        )
        self._raise_for_status(response, op="list_services")
        body = _parse_json_or_raise(response, op="list_services")
        return body if isinstance(body, list) else []

    async def list_providers(
        self, *, tenant_id: UUID
    ) -> list[dict[str, Any]]:
        """GET /api/v1/providers — list providers for the tenant.

        Like ``list_services``, the EA backend filters to the JWT
        ``tenant_id`` claim (ADR-024), so the response only contains the
        calling tenant's providers. Each provider dict carries at least
        ``id`` (int) and ``services`` (list of service ids); ``email`` /
        ``firstName`` / ``lastName`` support branch matching during the
        provisioning write-back sync.
        """
        self._check_config()
        response = await self._request(
            method="GET",
            url=self._url("api/v1/providers"),
            tenant_id=tenant_id,
            op="list_providers",
        )
        self._raise_for_status(response, op="list_providers")
        body = _parse_json_or_raise(response, op="list_providers")
        return body if isinstance(body, list) else []

    async def get_settings(
        self, *, tenant_id: UUID
    ) -> list[dict[str, Any]]:
        """GET /api/v1/settings — the tenant's EA settings rows.

        Returns EA's ``[{"name": ..., "value": ...}, ...]`` collection
        verbatim (tenant-scoped by the JWT claim per ADR-024). Used by the
        settings-sync service to mirror EA's booking policy + schedule into
        the Polaris ``tenants.settings`` cache the bot reads. Empty list if EA
        returns a non-array body.
        """
        self._check_config()
        response = await self._request(
            method="GET",
            url=self._url("api/v1/settings"),
            tenant_id=tenant_id,
            params={"length": _EA_FETCH_ALL_LENGTH},
            op="get_settings",
        )
        self._raise_for_status(response, op="get_settings")
        body = _parse_json_or_raise(response, op="get_settings")
        return body if isinstance(body, list) else []

    async def list_blocked_periods(
        self, *, tenant_id: UUID
    ) -> list[dict[str, Any]]:
        """GET /api/v1/blocked_periods — the tenant's blocked date ranges.

        EA api-encodes each row to ``{id, name, start, end, notes}`` (ISO8601
        ``start``/``end`` datetimes). Tenant-scoped by the JWT claim. Mirrored
        into ``ea_calendar.blocked_periods`` for the portal read-only panel.
        Empty list if EA returns a non-array body.
        """
        self._check_config()
        response = await self._request(
            method="GET",
            url=self._url("api/v1/blocked_periods"),
            tenant_id=tenant_id,
            params={"length": _EA_FETCH_ALL_LENGTH},
            op="list_blocked_periods",
        )
        self._raise_for_status(response, op="list_blocked_periods")
        body = _parse_json_or_raise(response, op="list_blocked_periods")
        return body if isinstance(body, list) else []

    async def list_availabilities(
        self,
        *,
        tenant_id: UUID,
        service_id: int,
        provider_id: int,
        date_from: date,
        date_to: date,
    ) -> list[str]:
        """GET /api/v1/availabilities — open slot times.

        EA returns an array of ISO8601 datetime strings (the slot start
        times). Caller renders these into the bot UI.
        """
        self._check_config()
        response = await self._request(
            method="GET",
            url=self._url("api/v1/availabilities"),
            tenant_id=tenant_id,
            params={
                "service_id": service_id,
                "provider_id": provider_id,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
            },
            op="list_availabilities",
        )
        self._raise_for_status(response, op="list_availabilities")
        body = _parse_json_or_raise(response, op="list_availabilities")
        return body if isinstance(body, list) else []

    async def create_appointment(
        self,
        *,
        tenant_id: UUID,
        service_id: int,
        provider_id: int,
        customer: dict[str, Any],
        start_datetime: str,
        end_datetime: str,
        notes: str = "",
    ) -> dict[str, Any]:
        """POST /api/v1/appointments — book a slot.

        ``customer`` is a dict matching EA's expected customer shape
        (first_name, last_name, email, phone, ...). EA either matches
        by email or creates a new customer row, both tenant-scoped.
        """
        self._check_config()
        body = {
            "service_id": service_id,
            "provider_id": provider_id,
            "customer": customer,
            "start_datetime": start_datetime,
            "end_datetime": end_datetime,
            "notes": notes,
        }
        response = await self._request(
            method="POST",
            url=self._url("api/v1/appointments"),
            tenant_id=tenant_id,
            json_body=body,
            op="create_appointment",
        )
        self._raise_for_status(response, op="create_appointment")
        appointment = _parse_json_or_raise(response, op="create_appointment")
        # silent-failure-hunter SEV-2: a broken EA build returning 201 with
        # no id silently produces appointment_id=None downstream, then
        # cancel_appointment(appointment_id=None) hits .../appointments/None
        # and 404s. Assert before returning so the failure surfaces at the
        # right layer with EA context.
        if not isinstance(appointment, dict) or not isinstance(
            appointment.get("id"), int
        ):
            raise EABookingStructuralError(
                "EA create_appointment returned 2xx with no integer id",
                status_code=response.status_code,
            )
        return appointment

    async def cancel_appointment(
        self, *, tenant_id: UUID, appointment_id: int
    ) -> None:
        """DELETE /api/v1/appointments/{id}.

        Idempotent at the EA layer — DELETE on an already-cancelled
        appointment returns 204 (the upstream EA pattern). We treat
        404 as a hard error since the caller indicated a specific id.
        """
        self._check_config()
        response = await self._request(
            method="DELETE",
            url=self._url(f"api/v1/appointments/{appointment_id}"),
            tenant_id=tenant_id,
            op="cancel_appointment",
        )
        self._raise_for_status(response, op="cancel_appointment")

    async def reschedule_appointment(
        self,
        *,
        tenant_id: UUID,
        appointment_id: int,
        start_datetime: str,
        end_datetime: str,
    ) -> dict[str, Any]:
        """PUT /api/v1/appointments/{id} — move the slot times.

        Returns the updated appointment dict.
        """
        self._check_config()
        response = await self._request(
            method="PUT",
            url=self._url(f"api/v1/appointments/{appointment_id}"),
            tenant_id=tenant_id,
            json_body={
                "start_datetime": start_datetime,
                "end_datetime": end_datetime,
            },
            op="reschedule_appointment",
        )
        self._raise_for_status(response, op="reschedule_appointment")
        body = _parse_json_or_raise(response, op="reschedule_appointment")
        if not isinstance(body, dict):
            raise EABookingStructuralError(
                "EA reschedule_appointment returned non-object body",
                status_code=response.status_code,
            )
        return body


    # -----------------------------------------------------------------
    # Internal provisioning verbs (HMAC-authed, NOT the tenant JWT)
    # -----------------------------------------------------------------

    def _check_provisioning_config(self) -> None:
        """Fail-closed on placeholder / empty provisioning config."""
        if not self.base_url:
            raise EABookingConfigError("EA_API_BASE_URL is unset.")
        secret = self.provisioning_hmac_secret
        if not secret or secret.startswith("placeholder"):
            raise EABookingConfigError(
                "EA_PROVISIONING_HMAC_SECRET is unset or placeholder. "
                "Set it to the same value as the EA fork's "
                "POLARIS_WEBHOOK_SECRET."
            )

    def _sign(self, raw_body: bytes) -> str:
        """X-Polaris-Signature value: 'sha256=<hexdigest>' over the raw body."""
        digest = hmac.new(
            self.provisioning_hmac_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return f"sha256={digest}"

    async def _request_hmac(
        self,
        *,
        method: str,
        url: str,
        raw_body: bytes,
        op: str,
    ) -> httpx.Response:
        """Single HMAC-signed request — no retry budget (mirrors _request).

        The signature MUST cover the exact bytes sent, so we pass
        ``content=raw_body`` (not ``json=``) — httpx would otherwise
        re-serialise and the EA-side ``php://input`` bytes would diverge
        from what we signed, yielding a 401.
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Polaris-Signature": self._sign(raw_body),
        }
        try:
            async with _get_client(self.timeout_s) as client:
                return await client.request(
                    method, url, content=raw_body, headers=headers,
                    timeout=self.timeout_s,
                )
        except httpx.TimeoutException as exc:
            raise EABookingTransientError(f"EA {op} timed out: {exc}") from exc
        except httpx.ConnectError as exc:
            raise EABookingTransientError(f"EA {op} connect error: {exc}") from exc

    async def provision_admin(
        self,
        *,
        tenant_id: UUID,
        slug: str,
        email: str,
        first_name: str,
        last_name: str,
        username: str = "",
        company_name: str = "",
        company_link: str = "",
        timezone: str = "",
    ) -> dict[str, Any]:
        """POST /api/v1/internal/tenants/provision.

        Creates (idempotently) an EA admin + default provider + default
        service for the tenant, seeds the tenant's default EA settings, and
        emails the admin a password-reset link. Returns
        ``{admin_user_id, provider_id, service_id, username}``.

        ``username`` (optional) is the operator-chosen EA admin login. When
        empty the EA side derives ``admin-<slug>``.

        ``company_name`` / ``company_link`` / ``timezone`` (optional) enrich
        the seeded EA settings; when empty the EA side falls back to sensible
        defaults (company name from first/last, link ``https://getpolaris.my``,
        timezone ``Asia/Kuala_Lumpur``). All optional fields are only included
        in the signed payload when set, so the wire format stays
        backward-compatible with older EA images.
        """
        self._check_provisioning_config()
        payload: dict[str, Any] = {
            "tenant_id": str(tenant_id),
            "slug": slug,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
        }
        if username:
            payload["username"] = username
        if company_name:
            payload["company_name"] = company_name
        if company_link:
            payload["company_link"] = company_link
        if timezone:
            payload["timezone"] = timezone
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = await self._request_hmac(
            method="POST",
            url=self._url("api/v1/internal/tenants/provision"),
            raw_body=raw,
            op="provision_admin",
        )
        self._raise_for_status(response, op="provision_admin")
        body = _parse_json_or_raise(response, op="provision_admin")
        if not isinstance(body, dict) or not isinstance(
            body.get("admin_user_id"), int
        ):
            raise EABookingStructuralError(
                "EA provision_admin returned 2xx with no admin_user_id",
                status_code=response.status_code,
            )
        return body

    async def sync_tenant(
        self,
        *,
        tenant_id: UUID,
        slug: str,
        status: str = "active",
    ) -> dict[str, Any]:
        """POST /api/v1/internal/tenants/sync.

        Upserts the tenant's row in EA's ``ea_polaris_tenants`` slug→tenant
        routing table (idempotent ON DUPLICATE KEY UPDATE). This is what makes
        the public booking page ``{base_url}/book/{slug}`` resolve: EA's
        ``Booking::slug`` → ``MY_Controller::from_slug`` reads this table and
        ``show_404()``s when the slug is absent. Provisioning seeds the EA
        account but does NOT register this row, so it must be synced explicitly
        or the bot's booking link 404s.

        ``slug`` MUST be the tenant's real slug (the same one the bot uses to
        build the booking link via ``public_booking_url``). ``status`` is
        ``"active"`` or ``"suspended"`` (EA 422s on anything else). Returns
        ``{ok, tenant_id, slug, status}``.
        """
        self._check_provisioning_config()
        payload: dict[str, Any] = {
            "tenant_id": str(tenant_id),
            "slug": slug,
            "status": status,
        }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = await self._request_hmac(
            method="POST",
            url=self._url("api/v1/internal/tenants/sync"),
            raw_body=raw,
            op="sync_tenant",
        )
        self._raise_for_status(response, op="sync_tenant")
        body = _parse_json_or_raise(response, op="sync_tenant")
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise EABookingStructuralError(
                "EA sync_tenant returned 2xx without ok=true",
                status_code=response.status_code,
            )
        return body

    async def deprovision(self, *, tenant_id: UUID) -> None:
        """DELETE /api/v1/internal/tenants/{tenant_id}/provision.

        Removes the tenant's EA admin/provider/service. Idempotent — a
        no-op on an already-removed tenant returns 200.
        """
        self._check_provisioning_config()
        # Empty body; the signature covers b"" and EA reads tenant_id from URL.
        response = await self._request_hmac(
            method="DELETE",
            url=self._url(f"api/v1/internal/tenants/{tenant_id}/provision"),
            raw_body=b"",
            op="deprovision",
        )
        self._raise_for_status(response, op="deprovision")


# =====================================================================
# Factory
# =====================================================================


@lru_cache(maxsize=1)
def get_ea_booking_client() -> EABookingClient:
    """Lazy singleton — constructed from env on first call.

    Tests monkeypatch via the factory after ``reset_client_for_testing()``.
    """
    settings = get_settings()
    return EABookingClient(
        base_url=settings.ea_api_base_url,
        jwt_signing_secret=settings.ea_jwt_signing_secret,
        jwt_audience=settings.ea_jwt_audience,
        jwt_issuer=settings.ea_jwt_issuer,
        jwt_ttl_seconds=settings.ea_jwt_ttl_seconds,
        timeout_s=settings.ea_api_timeout_s,
        max_retries=settings.ea_api_max_retries,
        provisioning_hmac_secret=settings.ea_provisioning_hmac_secret,
    )


def reset_client_for_testing() -> None:
    """Clear the lru_cache so tests can monkeypatch a fresh factory."""
    clear = getattr(get_ea_booking_client, "cache_clear", None)
    if callable(clear):
        clear()
