"""Demo runtime decision state machine — Demo System v3 (ADR-066).

:func:`resolve` is a **read-only** decision: given an inbound message it returns
a :class:`DemoDecision` telling the caller (pipeline/inbound path) which content
tenant to scope to and what side effect to perform. All writes — activating a
session, incrementing the message count, expiring, flagging the lead, and
dispatching the welcome/ended message — are performed by the caller so this
function stays pure-read and trivially unit-testable.

State machine (one demo EVER per contact; no mid-demo switching):

    active demo session for this contact?
      YES → time expired OR message cap reached → EXPIRE (revert to Polaris + flag lead)
            else                                 → CONTINUE_DEMO (answer as brand)
            (demo codes in the body are IGNORED while a demo is active)
      NO  → body contains a configured demo code (exact whole-token)?
              matched + contact already used a demo → REFUSE (answer as Polaris)
              matched + unused                       → ACTIVATE (welcome + start session)
              no match                               → DEFAULT (answer as Polaris)

The **gateway tenant** (``gateway_tenant_id`` — the tenant that OWNS the inbound
channel, i.e. the operator's real tenant) is the content scope for DEFAULT /
REFUSE / EXPIRE; the brand tenant is the content scope for ACTIVATE /
CONTINUE_DEMO. The demo system is a pure OVERLAY on the operator's existing
channels: a no-code message is answered by the operator's own bot, a demo code
switches content to the brand tenant, and expiry reverts to the operator.
``brand_demo`` is True only for brand turns (the caller uses it to skip audience
enrollment + quota — see plan M2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from polaris_api.db.pools import get_service_pool
from polaris_api.services import demo_sessions as ds
from polaris_api.services.demo_sessions import (
    ActiveDemoSession,
    DemoTenant,
)
from polaris_api.services.llm import _detect_language

DemoMode = Literal["default", "activate", "continue_demo", "expire", "refuse"]

# Alphanumeric runs only; punctuation + underscore are delimiters. This makes
# the match whole-token: "trgwatches1824" does NOT fire inside "mytrgwatches1824x"
# but does fire in "hi trgwatches1824!" or "code:trgwatches1824".
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Localized templates. {brand}/{hours}/{n} are interpolated by _render.
_MESSAGES: dict[str, dict[str, str]] = {
    "welcome": {
        "en": "Welcome to the {brand} demo! You have {hours} hours and {n} messages. Ask me anything.",
        "ms": "Selamat datang ke demo {brand}! Anda ada {hours} jam dan {n} mesej. Tanya saya apa-apa.",
        "zh-cn": "欢迎体验 {brand} 演示！您有 {hours} 小时和 {n} 条消息。有什么可以帮您？",
    },
    "ended": {
        "en": "Your {brand} demo has ended. Feel free to ask me anything about Polaris!",
        "ms": "Demo {brand} anda telah tamat. Boleh tanya saya apa-apa tentang Polaris!",
        "zh-cn": "您的 {brand} 演示已结束。欢迎向我咨询任何关于 Polaris 的问题！",
    },
    "already_used": {
        "en": "You've already used your demo. Ask me anything about Polaris and what it can do for you!",
        "ms": "Anda telah pun menggunakan demo anda. Tanya saya tentang Polaris dan apa yang ia boleh buat!",
        "zh-cn": "您已经使用过演示了。欢迎向我咨询 Polaris 以及它能为您做些什么！",
    },
}


@dataclass(frozen=True)
class DemoDecision:
    """The resolved demo decision for one inbound message."""

    mode: DemoMode
    content_tenant_id: UUID  # tenant the pipeline should scope to (RAG/persona)
    brand_demo: bool  # True for activate/continue → skip audience + quota
    direct_message: str | None = None  # canned reply the caller sends (welcome/ended/already_used)
    session: ActiveDemoSession | None = None  # set for continue_demo / expire
    demo: DemoTenant | None = None  # set for activate / refuse


def _tokens(body: str) -> list[str]:
    """Lowercased, whole-token candidates from the message body (deduped)."""
    seen: dict[str, None] = {}
    for tok in _TOKEN_RE.findall(body.lower()):
        seen.setdefault(tok, None)
    return list(seen)


def _render(key: str, *, brand: str, hours: int, n: int, language: str) -> str:
    table = _MESSAGES[key]
    template = table.get(language, table["en"])
    return template.format(brand=brand, hours=hours, n=n)


async def resolve(
    *,
    body: str | None,
    channel_user_id: str,
    gateway_tenant_id: UUID,
) -> DemoDecision:
    """Decide how to handle one inbound message. Read-only (no writes).

    ``gateway_tenant_id`` is the tenant that OWNS the inbound channel (the
    operator's real tenant — ``incoming.tenant_id``). The default / refuse /
    expire paths scope content to it, so a no-code message is answered by the
    operator's own bot and demo expiry reverts to it. It is REQUIRED (no default)
    so the demo overlay never silently reroutes a tenant's traffic to the wrong
    place — a demo code switches content to the brand tenant; everything else
    stays on the channel owner.
    """
    text = body or ""
    language = _detect_language(text) if text else "en"

    active = await ds.get_active_demo_session(channel_user_id)
    if active is not None:
        now = datetime.now(UTC)
        time_expired = now > active.expires_at
        cap_reached = active.message_count >= active.max_messages
        if time_expired or cap_reached:
            brand_name = await _brand_name(active.tenant_id)
            return DemoDecision(
                mode="expire",
                content_tenant_id=gateway_tenant_id,
                brand_demo=False,
                direct_message=_render(
                    "ended",
                    brand=brand_name,
                    hours=0,
                    n=active.max_messages,
                    language=language,
                ),
                session=active,
            )
        # Active and within caps → answer as the brand. Codes in body ignored.
        return DemoDecision(
            mode="continue_demo",
            content_tenant_id=active.tenant_id,
            brand_demo=True,
            session=active,
        )

    # No active session — look for a demo code (exact whole-token).
    for token in _tokens(text):
        demo = await ds.find_demo_tenant_by_code(token)
        if demo is None:
            continue
        if await ds.has_ever_used_demo(channel_user_id):
            return DemoDecision(
                mode="refuse",
                content_tenant_id=gateway_tenant_id,
                brand_demo=False,
                direct_message=_render(
                    "already_used",
                    brand=demo.display_name,
                    hours=demo.duration_hours,
                    n=demo.max_messages,
                    language=language,
                ),
                demo=demo,
            )
        return DemoDecision(
            mode="activate",
            content_tenant_id=demo.tenant_id,
            brand_demo=True,
            direct_message=_render(
                "welcome",
                brand=demo.display_name,
                hours=demo.duration_hours,
                n=demo.max_messages,
                language=language,
            ),
            demo=demo,
        )

    return DemoDecision(
        mode="default",
        content_tenant_id=gateway_tenant_id,
        brand_demo=False,
    )


async def _brand_name(tenant_id: UUID) -> str:
    """Best-effort display name for the expiry message; falls back to 'demo'."""
    try:
        pool = get_service_pool()
    except RuntimeError:
        return "demo"
    async with pool.acquire() as conn:
        name = await conn.fetchval(
            "SELECT display_name FROM tenants WHERE id = $1", tenant_id
        )
    return str(name) if name else "demo"
