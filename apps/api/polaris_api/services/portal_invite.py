"""Portal magic-link issuance from the admin side — Phase 9 PR-3 follow-up.

When the admin app creates a tenant via ``POST /api/admin/tenants``, the
new tenant_admin should receive a portal magic-link email
end-to-end without the operator copy/pasting URLs. This module owns
that dispatch.

Design:

  * Token mint mirrors NextAuth v4 EmailProvider's convention so the
    portal's existing custom adapter (apps/portal/lib/auth/adapter.ts)
    consumes the row exactly like a self-issued magic-link. Concretely:

        unhashed_token = secrets.token_hex(32)                  # 64 chars
        stored_hash    = sha256(unhashed_token + secret).hex()  # 64 chars
        identifier     = email.strip().lower()

    NextAuth v4 reads ``provider.secret`` (falls back to
    ``NEXTAUTH_SECRET``) for the hash input. We read
    ``settings.portal_nextauth_secret`` for the same value.

  * Row goes into the portal's ``magic_link_tokens`` table (migration
    052), not admin's ``admin_magic_link_tokens`` (055) — this is a
    PORTAL invite, not an admin sign-in.

  * Mail is sent with stdlib smtplib via a thread (asyncio.to_thread)
    to avoid blocking the event loop. We don't add aiosmtplib as a new
    dep just for the operator's once-a-month tenant-create flow; one
    short-lived SMTP connection per tenant create is fine.

  * Empty SMTP host OR empty portal_nextauth_secret → silent no-op
    (warning logged, audit row records `dispatched=False`). Dev
    environments without SMTP credentials boot cleanly; the operator
    is told to direct the new user to ``/login`` manually.

  * URL built per NextAuth v4 EmailProvider's callback shape:

      {portal_base}/api/auth/callback/email
        ?callbackUrl={portal_base}/dashboard
        &token={unhashed_token}
        &email={url_encoded_email}

    NextAuth's callback handler hashes the supplied token with the
    same secret and looks it up via the adapter's
    ``useVerificationToken``.

Threat model:

  * Token replay — adapter does atomic DELETE-RETURNING per migration
    052 + portal adapter; first click wins.
  * Token interception — same posture as the portal's own magic-links.
    Per ADR-030 magic-link is the entry; portal session JWT carries
    the rest.
  * Admin → portal SMTP credentials — admin API holds the SMTP creds
    in its env (separate from portal's; the same Chatwoot mailer is
    fine to share but the secret duplication is acknowledged). No
    cross-service HTTP call required.

Failure modes:

  * SMTP transport failure → the row is still in magic_link_tokens; the
    user can re-request via the portal /login flow and a new row will
    be issued (the old one expires + GCs). We do NOT roll back the
    tenant create on mail failure — tenant creation is the source of
    truth; the link can be re-sent.
  * Secret mismatch (portal_nextauth_secret diverges from portal's
    NEXTAUTH_SECRET) → portal will reject the click with "invalid
    token". Detect via a smoke after deploys.

Phase 9 PR-3 ships the service; the magic-link integration is wired
from admin_tenants.py::create_tenant.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import quote, urlencode

import asyncpg

from polaris_api.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InviteResult:
    """Outcome of an invite issuance attempt.

    ``dispatched`` is True only when BOTH the row was inserted AND the
    mail was sent without raising. ``expires_at`` is always populated
    when ``token_inserted`` is True (i.e. the user CAN still finish
    sign-in even if mail failed, by requesting a fresh link).
    """

    dispatched: bool
    token_inserted: bool
    expires_at: datetime | None
    failure_reason: str | None


def _hash_token(unhashed: str, secret: str) -> str:
    """Mirror NextAuth v4 EmailProvider hashing: sha256(token + secret) hex."""
    return hashlib.sha256(f"{unhashed}{secret}".encode()).hexdigest()


def _build_callback_url(portal_base: str, token: str, email: str) -> str:
    """Build the URL NextAuth's GET /api/auth/callback/email expects.

    portal_base is normalised (trailing slash stripped). callbackUrl
    defaults to the portal's /dashboard landing — middleware will
    redirect onwards if the tenant is suspended or onboarding.
    """
    base = portal_base.rstrip("/")
    params = urlencode(
        {
            "callbackUrl": f"{base}/dashboard",
            "token": token,
            "email": email,
        },
        quote_via=quote,
    )
    return f"{base}/api/auth/callback/email?{params}"


def _build_email(
    *, recipient: str, sign_in_url: str, expiry_min: int, sender: str
) -> EmailMessage:
    """Compose the invite email (HTML + plain-text fallback).

    Copy is Polaris-branded + identical in tone to the portal's own
    magic-link mail (apps/portal/lib/auth/email-template.ts). Inline
    styles only — most email clients strip <style> blocks.
    """
    msg = EmailMessage()
    msg["Subject"] = "Sign in to your Polaris portal"
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(

            "Sign in to Polaris\n\n"
            f"Click the link below to sign in. It works once and expires in {expiry_min} minutes.\n\n"
            f"{sign_in_url}\n\n"
            "If you didn't request this email, you can safely ignore it.\n"

    )
    msg.add_alternative(
        f"""<!DOCTYPE html>
<html lang="en">
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0f172a;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f8fafc;">
    <tr><td align="center" style="padding:32px 16px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="480" style="max-width:480px;width:100%;background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;">
        <tr><td style="padding:32px 32px 8px 32px;">
          <p style="margin:0 0 4px 0;font-size:12px;letter-spacing:0.12em;text-transform:uppercase;color:#64748b;">POLARIS</p>
          <h1 style="margin:0;font-size:20px;font-weight:600;color:#0f172a;">Sign in to your Polaris portal</h1>
        </td></tr>
        <tr><td style="padding:16px 32px;">
          <p style="margin:0 0 16px 0;font-size:14px;line-height:1.6;color:#334155;">
            Click the button below to sign in. The link will work once and expires in {expiry_min} minutes.
          </p>
          <p style="margin:0;">
            <a href="{sign_in_url}" style="display:inline-block;padding:12px 24px;background:#0f172a;color:#ffffff;text-decoration:none;border-radius:8px;font-size:14px;font-weight:500;">Sign in to Polaris</a>
          </p>
        </td></tr>
        <tr><td style="padding:16px 32px 32px 32px;">
          <p style="margin:0 0 8px 0;font-size:12px;line-height:1.6;color:#64748b;">Or copy this URL into your browser:</p>
          <p style="margin:0;font-size:12px;line-height:1.6;color:#475569;word-break:break-all;"><a href="{sign_in_url}" style="color:#475569;text-decoration:underline;">{sign_in_url}</a></p>
        </td></tr>
        <tr><td style="padding:0 32px 32px 32px;border-top:1px solid #e2e8f0;">
          <p style="margin:24px 0 0 0;font-size:12px;line-height:1.6;color:#94a3b8;">If you didn't request this email, you can safely ignore it &mdash; no one can sign in without clicking the link.</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>""",
        subtype="html",
    )
    return msg


def _send_mail_blocking(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    timeout: float,
    message: EmailMessage,
) -> None:
    """Sync SMTP send; called from asyncio.to_thread to avoid blocking.

    Uses STARTTLS (port 587 default). If the upstream rejects, smtplib
    raises which the caller catches and surfaces as a failure_reason.
    """
    with smtplib.SMTP(host=host, port=port, timeout=timeout) as smtp:
        smtp.ehlo()
        try:
            smtp.starttls()
            smtp.ehlo()
        except smtplib.SMTPException:
            # MailHog and some dev SMTP servers don't advertise STARTTLS;
            # proceed in plain-text for local/E2E environments only.
            pass
        if user:
            smtp.login(user, password)
        smtp.send_message(message)


async def issue_portal_invite(
    conn: asyncpg.Connection,
    *,
    recipient_email: str,
) -> InviteResult:
    """Issue a portal magic-link for the given email.

    Steps:
      1. Validate config; no-op + warning when secrets / SMTP unset.
      2. Generate token + hash + expiry timestamp.
      3. INSERT into magic_link_tokens via the supplied service-pool
         connection (caller is responsible for transactionality).
      4. Compose URL + email.
      5. Send via smtplib in a worker thread.
      6. Return InviteResult.

    The caller (admin_tenants.create_tenant) records an audit row
    describing the outcome regardless of dispatch success.
    """
    settings = get_settings()
    secret = settings.portal_nextauth_secret
    smtp_host = settings.portal_invite_smtp_host

    normalized_email = recipient_email.strip().lower()
    if not normalized_email:
        return InviteResult(False, False, None, "empty_recipient")

    if not secret:
        logger.warning(
            "[portal_invite] PORTAL_NEXTAUTH_SECRET unset; magic-link dispatch "
            "skipped. New tenant_admin must visit %s/login and request a link.",
            settings.portal_base_url,
        )
        return InviteResult(False, False, None, "secret_unset")

    unhashed = secrets.token_hex(32)
    stored_hash = _hash_token(unhashed, secret)
    expires_at = datetime.now(UTC) + timedelta(
        minutes=settings.portal_invite_expiry_minutes
    )

    try:
        await conn.execute(
            """
            INSERT INTO magic_link_tokens (identifier, token, expires)
            VALUES ($1, $2, $3)
            """,
            normalized_email,
            stored_hash,
            expires_at,
        )
    except Exception:  # noqa: BLE001 — surface labelled failure context
        logger.exception(
            "[portal_invite] magic_link_tokens INSERT failed for %s; "
            "dispatch aborted.",
            normalized_email,
        )
        return InviteResult(False, False, None, "db_insert_failed")

    if not smtp_host:
        logger.warning(
            "[portal_invite] PORTAL_INVITE_SMTP_HOST unset; row inserted but "
            "mail NOT sent for %s. Operator must direct user to /login.",
            normalized_email,
        )
        return InviteResult(False, True, expires_at, "smtp_unset")

    sign_in_url = _build_callback_url(
        settings.portal_base_url, unhashed, normalized_email
    )
    message = _build_email(
        recipient=normalized_email,
        sign_in_url=sign_in_url,
        expiry_min=settings.portal_invite_expiry_minutes,
        sender=settings.portal_invite_smtp_from,
    )

    try:
        await asyncio.to_thread(
            _send_mail_blocking,
            host=smtp_host,
            port=settings.portal_invite_smtp_port,
            user=settings.portal_invite_smtp_user,
            password=settings.portal_invite_smtp_password,
            timeout=settings.portal_invite_smtp_timeout_s,
            message=message,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "[portal_invite] SMTP send failed for %s via %s:%s. Token row "
            "remains so user can still finish sign-in by requesting a fresh "
            "link from the portal.",
            normalized_email,
            smtp_host,
            settings.portal_invite_smtp_port,
        )
        return InviteResult(False, True, expires_at, "smtp_send_failed")

    return InviteResult(True, True, expires_at, None)
