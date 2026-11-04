"""Shared channel-kind -> tenant_secrets.kind base mapping.

Hoisted out of ``lazy_provision.py`` / ``reconcile_channels.py`` /
``provision.py`` / ``admin_tenants.py``, which each carried their own copy of
this map (a "keep in sync" comment in every file, no enforcement).

This base map covers exactly the three channel kinds whose access token is
auto-captured from Chatwoot: whatsapp, messenger, instagram_login. It is used
as a ``.get(channel)`` BEHAVIORAL GATE in ``lazy_provision.py`` and
``reconcile_channels.py`` — only kinds present as keys get token-capture /
token-check treatment. Do NOT add telegram (or any other kind) to this base
map: doing so would change behavior for those two consumers, which have never
treated telegram as a checkable/capturable-token channel.

Callers that need telegram too (``provision.py``, whose scope also covers the
bespoke @BotFather-issued telegram token; ``admin_tenants.py``, which reads
back a masked token for every channel kind on the tenant detail page) extend
this base map locally, e.g.::

    _CHANNEL_SECRET_KIND: dict[str, str] = {
        **BASE_CHANNEL_SECRET_KINDS,
        "telegram": "telegram_bot_token",
    }
"""

from __future__ import annotations

BASE_CHANNEL_SECRET_KINDS: dict[str, str] = {
    "whatsapp": "meta_waba_access_token",
    "messenger": "meta_fb_page_access_token",
    "instagram_login": "meta_ig_login_access_token",
}
