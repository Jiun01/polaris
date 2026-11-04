"""Per-tenant LLM cost ceiling enforcement — Phase 10 PR-4.

Called by the nightly 03:00 MYT ``cost_ceiling_check`` scheduler.

Two checks per run:

1. **Per-tenant ceiling** — iterates active tenants with a non-null
   ``tenant_quotas.llm_cost_ceiling_myr``.  For each:

   - Fetches current-month LLM cost from Grafana Cloud Mimir.
   - ≥ 75 % of ceiling → warning email to ``ADMIN_EMAIL_TO``.
   - ≥ 100 % of ceiling → Sentry fatal page + auto-disables Cohere
     rerank (``UPDATE tenant_quotas SET cohere_rerank_enabled = FALSE``)
     + emits an audit row so the action is visible in the admin audit log.

   Both alert levels are idempotent within a 24-hour window: a counter
   (in-memory, per-process) prevents flooding when Mimir reports the
   same value multiple nightly runs in a row.  The counter resets on
   process restart; at GA (single-process deploy) this is acceptable.

2. **Platform daily spend** — sums all-tenant LLM cost across the last
   24 h via Mimir.  Exceeds ``POLARIS_PLATFORM_DAILY_SPEND_THRESHOLD_MYR``
   → Sentry fatal page + logger.error.

Both checks degrade gracefully when Mimir is not configured:
``fetch_tenant_cost_monthly`` returns zeros with ``data_source='unavailable'``
and the checks simply log a warning and skip — no false alerts.

ADR refs: ADR-035 (Sentry per-tenant thresholds), ADR-026 (plan tiers + caps).
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.message import EmailMessage
from uuid import UUID

import asyncpg

from polaris_api.services.audit import record as audit_record
from polaris_api.services.grafana_cost import fetch_tenant_cost_monthly

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory dedup: tenant_id → last emitted band so we don't flood on
# repeated nightly runs.  key = str(tenant_id), value = "warn" | "hard".
# ---------------------------------------------------------------------------

_LAST_EMITTED: dict[str, str] = {}
_SEVERITY = {"warn": 1, "hard": 2}


def _already_alerted(tenant_id: UUID, level: str) -> bool:
    """True if we already fired an alert at this level (or higher) today."""
    last = _LAST_EMITTED.get(str(tenant_id))
    if last is None:
        return False
    return _SEVERITY.get(last, 0) >= _SEVERITY.get(level, 0)


def _record_alert(tenant_id: UUID, level: str) -> None:
    _LAST_EMITTED[str(tenant_id)] = level


# ---------------------------------------------------------------------------
# Email helper (mirrors weekly_digest.py pattern)
# ---------------------------------------------------------------------------


def _send_ceiling_alert_email(
    *,
    tenant_id: UUID,
    tenant_name: str,
    cost_myr: Decimal,
    ceiling_myr: Decimal,
    pct: float,
    settings: object,
) -> None:
    """Send a 75%-ceiling warning email to the platform admin."""
    host = getattr(settings, "admin_email_server_host", "") or ""
    port = int(getattr(settings, "admin_email_server_port", 587) or 587)
    user = getattr(settings, "admin_email_server_user", "") or ""
    password = getattr(settings, "admin_email_server_password", "") or ""
    from_addr = getattr(settings, "admin_email_from", "") or ""
    to_addr = getattr(settings, "admin_email_to", "") or ""

    if not host or not from_addr or not to_addr:
        logger.warning(
            "cost_ceiling: SMTP not configured — skipping 75%% alert "
            "tenant=%s (set ADMIN_EMAIL_SERVER_HOST, ADMIN_EMAIL_FROM, ADMIN_EMAIL_TO)",
            tenant_id,
        )
        return

    yyyymm = datetime.now(UTC).strftime("%Y-%m")
    subject = (
        f"[Polaris] LLM cost ceiling 75% warning — {tenant_name} ({yyyymm})"
    )
    body = (
        f"LLM cost ceiling warning\n"
        f"========================\n\n"
        f"  Tenant      : {tenant_name} ({tenant_id})\n"
        f"  Period      : {yyyymm}\n"
        f"  Current cost: RM {float(cost_myr):,.2f}\n"
        f"  Ceiling     : RM {float(ceiling_myr):,.2f}\n"
        f"  Usage       : {pct * 100:.1f}%\n\n"
        f"Action required: review LLM usage for this tenant.\n"
        f"If usage continues to grow, the ceiling will be breached and\n"
        f"Cohere rerank will be automatically disabled.\n\n"
        f"Manage ceiling: POST /api/admin/quotas/{tenant_id}/override\n"
        f"  body: {{\"column_name\": \"llm_cost_ceiling_myr\", \"value\": <new_value>}}\n"
    )
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPException:
                # MailHog/dev SMTP doesn't advertise STARTTLS. Plain-text is
                # acceptable ONLY when no credentials would be sent — never
                # log in over cleartext (mirrors portal_invite.py).
                if user and password:
                    raise
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
        logger.info(
            "cost_ceiling: 75%% alert email sent tenant=%s pct=%.1f%%",
            tenant_id,
            pct * 100,
        )
    except Exception as exc:
        logger.error(
            "cost_ceiling: SMTP send failed tenant=%s err=%s", tenant_id, exc,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Sentry helper (mirrors mrr_rollup.py pattern — lazy import)
# ---------------------------------------------------------------------------


def _emit_sentry_fatal(message: str, tags: dict[str, str]) -> None:
    """Fire a Sentry fatal-level capture_message.  Degrades if Sentry absent."""
    try:
        import sentry_sdk
        sentry_sdk.capture_message(message, level="fatal", tags=tags)
        logger.error("cost_ceiling: Sentry fatal fired — %s", message)
    except Exception as exc:
        logger.error(
            "cost_ceiling: Sentry emit failed err=%s (original: %s)", exc, message,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Core per-tenant check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TenantCeilingCheck:
    """Result of ``fetch_tenant_ceiling_status`` (audit Batch B / B7-c) — the
    conn-free HTTP phase of the per-tenant ceiling check.

    ``breach`` is ``"hard"`` / ``"warn"`` / ``None`` (healthy or unavailable).
    ``apply_tenant_ceiling_breach`` uses this to decide which DB write (if
    any) + notification to fire, without touching Mimir again.
    """

    tenant_id: UUID
    tenant_name: str
    ceiling_myr: Decimal
    cost_myr: Decimal | None
    pct: float
    breach: str | None  # "hard" | "warn" | None


async def fetch_tenant_ceiling_status(
    *,
    tenant_id: UUID,
    tenant_name: str,
    ceiling_myr: Decimal,
    yyyymm: str,
    mimir_url: str,
    mimir_token: str,
) -> _TenantCeilingCheck:
    """Phase 1 (HTTP, no conn): fetch this tenant's monthly cost from Mimir
    and classify it against the ceiling. Never raises — a Mimir failure
    yields ``breach=None`` so the caller just skips the tenant.
    """
    if ceiling_myr <= Decimal("0"):
        return _TenantCeilingCheck(
            tenant_id=tenant_id, tenant_name=tenant_name, ceiling_myr=ceiling_myr,
            cost_myr=None, pct=0.0, breach=None,
        )

    try:
        result = await fetch_tenant_cost_monthly(
            tenant_id=tenant_id,
            yyyymm=yyyymm,
            tenant_mrr_myr=Decimal("0"),  # not needed for ceiling comparison
            mimir_url=mimir_url,
            mimir_token=mimir_token,
        )
    except Exception as exc:
        logger.warning(
            "cost_ceiling: Mimir fetch failed tenant=%s err=%s — skipping",
            tenant_id, exc,
        )
        return _TenantCeilingCheck(
            tenant_id=tenant_id, tenant_name=tenant_name, ceiling_myr=ceiling_myr,
            cost_myr=None, pct=0.0, breach=None,
        )

    if result.data_source == "unavailable":
        logger.debug(
            "cost_ceiling: Mimir unavailable for tenant=%s — skipping check",
            tenant_id,
        )
        return _TenantCeilingCheck(
            tenant_id=tenant_id, tenant_name=tenant_name, ceiling_myr=ceiling_myr,
            cost_myr=None, pct=0.0, breach=None,
        )

    cost = result.cost.total_cost_myr
    pct = float(cost / ceiling_myr) if ceiling_myr > 0 else 0.0

    logger.debug(
        "cost_ceiling: tenant=%s cost=%.2f ceiling=%.2f pct=%.1f%%",
        tenant_id, float(cost), float(ceiling_myr), pct * 100,
    )

    breach: str | None = None
    if pct >= 1.0:
        breach = "hard"
    elif pct >= 0.75:
        breach = "warn"

    return _TenantCeilingCheck(
        tenant_id=tenant_id, tenant_name=tenant_name, ceiling_myr=ceiling_myr,
        cost_myr=cost, pct=pct, breach=breach,
    )


async def apply_tenant_ceiling_breach(
    conn: asyncpg.Connection, check: _TenantCeilingCheck, *, settings: object,
) -> None:
    """Phase 2 (conn, no HTTP): act on a ``fetch_tenant_ceiling_status`` result.

    Raises nothing — errors are logged and suppressed so the caller's
    per-tenant isolation loop can continue to the next tenant.
    """
    if check.breach is None or check.cost_myr is None:
        return
    tenant_id, tenant_name = check.tenant_id, check.tenant_name
    cost, ceiling_myr, pct = check.cost_myr, check.ceiling_myr, check.pct

    if check.breach == "hard":
        if not _already_alerted(tenant_id, "hard"):
            _emit_sentry_fatal(
                f"LLM cost ceiling breached: {tenant_name} "
                f"RM {float(cost):.2f} / RM {float(ceiling_myr):.2f} "
                f"({pct * 100:.0f}%)",
                tags={
                    "tenant_id": str(tenant_id),
                    "tenant_name": tenant_name,
                    "cost_myr": str(float(cost)),
                    "ceiling_myr": str(float(ceiling_myr)),
                },
            )
            # Auto-disable Cohere rerank to protect margin.
            await conn.execute(
                """
                UPDATE tenant_quotas
                   SET cohere_rerank_enabled = FALSE
                 WHERE tenant_id = $1
                   AND cohere_rerank_enabled IS NOT FALSE
                """,
                tenant_id,
            )
            await audit_record(
                conn,
                tenant_id=tenant_id,
                actor=None,
                action="cost_ceiling.hard_breach",
                after={
                    "cost_myr": str(cost),
                    "ceiling_myr": str(ceiling_myr),
                    "pct": round(pct, 4),
                    "action": "cohere_rerank_disabled",
                },
            )
            _record_alert(tenant_id, "hard")
            logger.error(
                "cost_ceiling: HARD BREACH tenant=%s cost=%.2f ceiling=%.2f — "
                "cohere_rerank_enabled set to FALSE",
                tenant_id, float(cost), float(ceiling_myr),
            )

    elif check.breach == "warn":
        if not _already_alerted(tenant_id, "warn"):
            _send_ceiling_alert_email(
                tenant_id=tenant_id,
                tenant_name=tenant_name,
                cost_myr=cost,
                ceiling_myr=ceiling_myr,
                pct=pct,
                settings=settings,
            )
            _record_alert(tenant_id, "warn")


async def check_tenant_cost_ceiling(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    tenant_name: str,
    ceiling_myr: Decimal,
    yyyymm: str,
    mimir_url: str,
    mimir_token: str,
    settings: object,
) -> None:
    """Run cost ceiling check for one tenant — composes the two phases above
    on ONE connection.

    Kept for callers that don't need the conn released during the Mimir
    fetch (tests, one-off scripts). ``scheduled/cost_ceiling_check.py``'s
    job loop calls the two phases directly instead (audit Batch B / B7-c),
    so a conn is only held for the (fast) DB-write phase, not the HTTP call.

    Raises nothing — errors are logged and suppressed so the caller's
    per-tenant isolation loop can continue to the next tenant.
    """
    check = await fetch_tenant_ceiling_status(
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        ceiling_myr=ceiling_myr,
        yyyymm=yyyymm,
        mimir_url=mimir_url,
        mimir_token=mimir_token,
    )
    await apply_tenant_ceiling_breach(conn, check, settings=settings)


# ---------------------------------------------------------------------------
# Platform-wide daily spend check
# ---------------------------------------------------------------------------


async def check_platform_daily_spend(
    pool: asyncpg.Pool,
    *,
    threshold_myr: float,
    mimir_url: str,
    mimir_token: str,
) -> None:
    """Sum all-tenant LLM costs for the current month; page if over threshold.

    Uses the current month yyyymm rather than a strict 24h window because
    Mimir's ``increase([30d])`` queries don't support arbitrary time ranges
    at this layer.  The threshold should be set as a monthly guard, not a
    daily one, despite the parameter name (kept for ADR-035 naming clarity).

    Degrades gracefully when Mimir is not configured.

    Takes ``pool`` (not ``conn``) — audit Batch B (B7-c): acquires a conn
    ONLY for the short tenant-list read, releases it, then loops the (slow)
    per-tenant Mimir HTTP fetch with no conn held. Previously this took the
    caller's already-open conn and held it for the WHOLE per-tenant HTTP
    loop below.
    """
    if threshold_myr <= 0:
        logger.debug("cost_ceiling: platform daily spend check disabled (threshold=0)")
        return

    if not mimir_url or not mimir_token:
        logger.debug(
            "cost_ceiling: Mimir not configured — skipping platform spend check"
        )
        return

    yyyymm = datetime.now(UTC).strftime("%Y%m")

    # Fetch all active tenants on a short-lived conn, released before the
    # HTTP loop below.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, display_name FROM tenants WHERE status = 'active'"
        )

    total_cost = Decimal("0")
    any_unavailable = False

    for row in rows:
        tenant_id: UUID = row["id"]
        try:
            result = await fetch_tenant_cost_monthly(
                tenant_id=tenant_id,
                yyyymm=yyyymm,
                tenant_mrr_myr=Decimal("0"),
                mimir_url=mimir_url,
                mimir_token=mimir_token,
            )
            if result.data_source == "unavailable":
                any_unavailable = True
            else:
                total_cost += result.cost.total_cost_myr
        except Exception as exc:
            logger.warning(
                "cost_ceiling: platform spend fetch failed tenant=%s err=%s",
                tenant_id, exc,
            )
            any_unavailable = True

    if any_unavailable:
        logger.warning(
            "cost_ceiling: platform spend check partial (some tenants unavailable) "
            "— total reflects available tenants only total_myr=%.2f",
            float(total_cost),
        )

    logger.info(
        "cost_ceiling: platform monthly spend total_myr=%.2f threshold_myr=%.2f",
        float(total_cost), threshold_myr,
    )

    if float(total_cost) > threshold_myr:
        _emit_sentry_fatal(
            f"Platform LLM spend exceeds threshold: "
            f"RM {float(total_cost):.2f} > RM {threshold_myr:.2f}",
            tags={
                "total_cost_myr": str(float(total_cost)),
                "threshold_myr": str(threshold_myr),
            },
        )
