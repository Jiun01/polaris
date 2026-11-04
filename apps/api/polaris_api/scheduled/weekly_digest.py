"""Weekly MRR digest email — Phase 9 PR-11.

Runs Monday 09:00 Asia/Kuala_Lumpur.

Sends a plain-text email to the operator with the current total MRR.

Email goes via SMTP using the same ADMIN_EMAIL_SERVER_* settings as other
admin notification paths. If any SMTP setting is unset / placeholder, the
job logs a warning and skips silently — consistent with the portal invite
mailer's no-op-on-missing-config pattern.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)
_scheduler = None


def _build_digest_email(mrr: float, from_addr: str, to_addr: str) -> EmailMessage:
    subject = f"[Polaris] Weekly MRR digest — RM {mrr:,.2f}"
    body = (
        f"Weekly MRR digest\n"
        f"===========================================\n\n"
        f"  Current MRR : RM {mrr:,.2f}\n\n"
        f"Sent every Monday 09:00 MYT.\n"
    )
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)
    return msg


def _send_digest(mrr: float, settings: object) -> None:
    host = getattr(settings, "admin_email_server_host", "") or ""
    port = int(getattr(settings, "admin_email_server_port", 587) or 587)
    user = getattr(settings, "admin_email_server_user", "") or ""
    password = getattr(settings, "admin_email_server_password", "") or ""
    from_addr = getattr(settings, "admin_email_from", "") or ""
    to_addr = getattr(settings, "admin_email_to", "") or ""

    if not host or not from_addr or not to_addr:
        logger.warning(
            "weekly_digest: SMTP not configured — skipping "
            "(set ADMIN_EMAIL_SERVER_HOST, ADMIN_EMAIL_FROM, ADMIN_EMAIL_TO)"
        )
        return

    msg = _build_digest_email(mrr, from_addr, to_addr)
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
        logger.info("weekly_digest: email sent mrr_myr=%.2f to=%s", mrr, to_addr)
    except Exception as exc:
        logger.error("weekly_digest: SMTP send failed err=%s", exc, exc_info=True)


async def _run(pool: object) -> None:
    """Compute MRR + send the weekly digest.

    Module-level (not a closure) so it's directly unit-testable — mirrors
    ``ea_settings_sync.py``'s ``_sync_job(service_pool, ...)`` style.
    """
    from polaris_api.config import get_settings
    from polaris_api.services import mrr_rollup

    logger.info("weekly_digest: Monday digest job starting")
    try:
        # Read-only: the digest only needs the current MRR. Snapshots are
        # written by the nightly rollup job, not here.
        async with pool.acquire() as conn:  # type: ignore[attr-defined]
            result = await mrr_rollup.compute_mrr(conn)
        mrr: float = result["mrr_myr"]
        settings = get_settings()
        # _send_digest does blocking smtplib I/O (up to 15s timeout) — run
        # it off the event loop thread so a slow/stalled SMTP connection
        # doesn't block the whole single-worker event loop (silent-failure
        # audit fix A3, 2026-07-02).
        await asyncio.to_thread(_send_digest, mrr, settings)
    except Exception as exc:
        logger.error("weekly_digest: job failed err=%s", exc, exc_info=True)


async def _weekly_job(pool: object) -> None:
    from polaris_api.scheduled._locking import JOB_LOCK_KEYS, run_with_advisory_lock

    await run_with_advisory_lock(
        pool,
        job_name="weekly_digest",
        lock_key=JOB_LOCK_KEYS["weekly_digest"],
        job=lambda: _run(pool),
    )


def register_weekly_digest_scheduler(pool: object) -> None:
    """Register Monday 09:00 MYT weekly digest job."""
    global _scheduler  # noqa: PLW0603
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("apscheduler not installed -- weekly digest scheduler disabled.")
        return

    async def _job() -> None:
        await _weekly_job(pool)

    _scheduler = AsyncIOScheduler(timezone="Asia/Kuala_Lumpur")
    _scheduler.add_job(
        _job,
        CronTrigger(day_of_week="mon", hour=9, minute=0, timezone="Asia/Kuala_Lumpur"),
        id="mrr_weekly_digest",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("weekly_digest: scheduled at 09:00 MYT on Mondays")


def shutdown_weekly_digest_scheduler() -> None:
    """Stop the weekly digest scheduler."""
    global _scheduler  # noqa: PLW0603
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
