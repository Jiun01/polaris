"""Operator-driven smoke test for Meta Messenger Profile push.

Phase 7 PR-7 — verifies ``services/messenger_profile.py`` against a real
sandbox FB Page. NOT in CI — needs real Meta credentials in env:

    META_FB_PAGE_ID=<sandbox page id>
    META_FB_PAGE_ACCESS_TOKEN=<short-lived or system-user token>

Builds an in-memory MessengerProfileBundle (3 sections), POSTs it to
``/v25.0/me/messenger_profile``, re-GETs to capture any drift, and
prints the result. No DB writes — bypasses the DB-routed
``push_profile`` helper and exercises the wire layer directly via
``httpx`` so the operator can validate the Meta contract independent
of Polaris's persistence layer.

Usage:

    cd apps/api
    uv run python scripts/smoke_messenger_profile.py

Expected output: ``status_code=200``, ``diff_against_remote=None`` on
a clean push. Any drift (Meta silently dropped a field) surfaces in
the printed diff so the operator can inspect Meta's locale-rule
behaviour before wiring real tenants.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime

import httpx
from polaris_api.config import get_settings
from polaris_shared_types.messenger_profile import (
    MessengerGetStarted,
    MessengerIcebreaker,
    MessengerPersistentMenu,
    MessengerProfileBundle,
    PersistentMenuCallToAction,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def _build_smoke_bundle() -> MessengerProfileBundle:
    """The three GA sections, plus an idiosyncratic icebreaker payload
    to verify Meta's locale handling on the round-trip diff."""
    return MessengerProfileBundle(
        ice_breakers=[
            MessengerIcebreaker(
                question="What are your hours?",
                payload="icebreaker:hours",
            ),
            MessengerIcebreaker(
                question="How do I book?",
                payload="icebreaker:book",
            ),
            MessengerIcebreaker(
                question="Talk to a human",
                payload="icebreaker:handoff",
            ),
        ],
        persistent_menu=[
            MessengerPersistentMenu(
                locale="default",
                composer_input_disabled=False,
                call_to_actions=[
                    PersistentMenuCallToAction(
                        type="postback",
                        title="Track order",
                        payload="menu:track_order",
                    ),
                    PersistentMenuCallToAction(
                        type="postback",
                        title="View catalog",
                        payload="menu:catalog",
                    ),
                    PersistentMenuCallToAction(
                        type="postback",
                        title="Speak to agent",
                        payload="menu:handoff",
                    ),
                ],
            ),
        ],
        get_started=MessengerGetStarted(payload="get_started"),
    )


async def _push_and_diff() -> int:
    page_id = os.environ.get("META_FB_PAGE_ID")
    token = os.environ.get("META_FB_PAGE_ACCESS_TOKEN")
    if not page_id or not token:
        sys.stderr.write(
            "ERROR: set META_FB_PAGE_ID + META_FB_PAGE_ACCESS_TOKEN in env.\n"
            "These are sandbox Meta credentials — never commit them.\n"
        )
        return 2
    settings = get_settings()
    version = settings.meta_graph_api_version
    url = f"https://graph.facebook.com/{version}/me/messenger_profile"
    bundle = _build_smoke_bundle()
    body = json.loads(bundle.model_dump_json(exclude_none=True))

    pushed_at = datetime.now(UTC).isoformat()
    logger.info("→ POST %s page_id=%s", url, page_id)
    async with httpx.AsyncClient(timeout=settings.messenger_profile_api_timeout_s) as cli:
        post = await cli.request(
            "POST", url, params={"access_token": token}, json=body
        )
    logger.info(
        "← POST status=%d body=%s",
        post.status_code,
        json.dumps(post.json() if post.headers.get("content-type", "").startswith("application/json") else {})[:300],
    )
    if post.status_code >= 400:
        sys.stderr.write(f"FAIL: Meta returned {post.status_code}\n")
        return 1

    logger.info("→ GET %s for round-trip diff", url)
    async with httpx.AsyncClient(timeout=settings.messenger_profile_api_timeout_s) as cli:
        get = await cli.request(
            "GET",
            url,
            params={
                "access_token": token,
                "fields": "ice_breakers,persistent_menu,get_started",
            },
        )
    logger.info("← GET status=%d", get.status_code)
    remote = {}
    if get.status_code == 200:
        payload = get.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            remote = data[0]

    diff: dict[str, object] = {}
    for key in ("ice_breakers", "persistent_menu", "get_started"):
        sent = body.get(key)
        rem = remote.get(key)
        if sent != rem:
            diff[key] = {"sent": sent, "remote": rem}

    print("===== SMOKE RESULT =====")
    print(f"pushed_at: {pushed_at}")
    print(f"meta_status (POST): {post.status_code}")
    print(f"meta_status (GET):  {get.status_code}")
    if not diff:
        print("diff_against_remote: NONE (clean push — Meta state matches sent)")
        return 0
    print("diff_against_remote:")
    print(json.dumps(diff, indent=2, default=str))
    print(
        "\nNOTE: a diff means Meta silently transformed/dropped fields. "
        "Common causes: emoji in locale-restricted icebreakers, length "
        "caps exceeded, or get_started payload reserved-word collision."
    )
    return 0


def main() -> int:
    return asyncio.run(_push_and_diff())


if __name__ == "__main__":
    raise SystemExit(main())
