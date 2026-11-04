"""Meta Graph /messenger_profile bundle — Pydantic SSOT (Phase 7 PR-1).

Mirrors Meta's documented contract for v21.0 (locked 2026-05-19). All
caps + max lengths are Meta's published limits; we enforce them client-
side so an oversized icebreaker fails locally with a useful error rather
than as a 400 from Meta.

Q-PHASE7-06: Phase 7 always PUTs the full bundle to Meta — there is no
partial-field push. ``MessengerProfileBundle`` is the single shape sent
in the request body. Field-scoped DELETE exists for fully clearing one
section.

If Meta extends the schema (e.g. adds ``home_url``, ``account_linking_url``,
``whitelisted_domains`` to the bundle), this file is the single update
site. The service module reads via these DTOs; no other code touches
the wire shape.

ConfigDict(extra='forbid') so an unknown field returned in a Meta GET
response fails our parser instead of being silently dropped. That's the
trigger for a Meta-schema-drift investigation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")


class MessengerIcebreaker(BaseModel):
    """One icebreaker entry in the messenger_profile.ice_breakers array.

    Shown as a tappable chip above the message-input box when a customer
    first opens the FB Messenger conversation with the tenant's Page.
    Tapping sends ``payload`` to the bot as a postback (not as the
    visible question text).
    """

    model_config = _FORBID

    question: str = Field(
        ...,
        min_length=1,
        max_length=80,
        description="Customer-visible prompt text (Meta cap: 80 chars).",
    )
    payload: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description=(
            "Bot-side postback payload when customer taps the icebreaker. "
            "Polaris convention: prefix with 'icebreaker:' for routing "
            "(e.g. 'icebreaker:hours'). Meta cap: 1000 chars."
        ),
    )


class PersistentMenuCallToAction(BaseModel):
    """One entry inside a persistent_menu.call_to_actions array.

    Two flavours: ``postback`` (carries a payload back to the bot) and
    ``web_url`` (opens a URL in the Messenger in-app browser).
    """

    model_config = _FORBID

    type: Literal["postback", "web_url"]
    title: str = Field(
        ...,
        min_length=1,
        max_length=30,
        description="Visible CTA label (Meta cap: 30 chars).",
    )
    payload: str | None = Field(
        None,
        max_length=1000,
        description="Required when type='postback'. Bot postback payload. Meta cap: 1000 chars.",
    )
    url: str | None = Field(
        None,
        max_length=2048,
        description="Required when type='web_url'. https-only per Meta policy. Meta cap: 2048 chars.",
    )


class MessengerPersistentMenu(BaseModel):
    """One locale-scoped persistent menu.

    ``locale='default'`` applies cross-locale (Meta's wildcard).
    Specific locales (e.g. ``locale='zh_CN'``) override the default for
    customers whose Messenger UI is in that locale.

    ``composer_input_disabled=True`` removes the free-text input box —
    customers can ONLY interact via the menu CTAs and icebreakers. Used
    by tenants with strictly menu-driven flows (booking-only, FAQ-only).
    """

    model_config = _FORBID

    locale: str = Field("default", description="Meta locale code or 'default'.")
    composer_input_disabled: bool = Field(
        False,
        description="If True, customer cannot type free text — menu-only interaction.",
    )
    call_to_actions: list[PersistentMenuCallToAction] = Field(
        ...,
        min_length=1,
        max_length=3,
        description="1-3 CTAs per Meta. Order = display order top-to-bottom in the menu.",
    )


class MessengerGetStarted(BaseModel):
    """The 'Get Started' button shown on the welcome screen.

    Customers who have never messaged the Page see a single CTA button
    on the empty conversation. Tapping fires this payload to the bot,
    which typically routes to the onboarding flow.
    """

    model_config = _FORBID

    payload: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description=(
            "Postback payload fired when customer taps the 'Get Started' "
            "button. Polaris convention: 'get_started' (literal string) "
            "or 'get_started:<flow_slug>'."
        ),
    )


class MessengerProfileBundle(BaseModel):
    """Full bundle PUT to Meta /messenger_profile.

    Q-PHASE7-06: Phase 7 always PUTs all three sections, even when the
    tenant only edited one. Simpler code, fewer Meta edge cases (partial
    PUT semantics are inconsistent across Meta SDK versions).

    Empty lists (``ice_breakers=[]``, ``persistent_menu=[]``) clear those
    sections in Meta. ``get_started=None`` is the only way to UNSET that
    section — a None means "feature not configured".
    """

    model_config = _FORBID

    ice_breakers: list[MessengerIcebreaker] = Field(
        default_factory=list,
        max_length=4,
        description="Up to 4 icebreakers per Meta. Empty list clears the section.",
    )
    persistent_menu: list[MessengerPersistentMenu] = Field(
        default_factory=list,
        description="Per-locale menus. Empty list clears the section.",
    )
    get_started: MessengerGetStarted | None = Field(
        None,
        description="Welcome-screen CTA. None means feature not configured / cleared.",
    )


class MessengerProfilePushResult(BaseModel):
    """Returned by services/messenger_profile.py::push_profile.

    ``diff_against_remote`` carries the partial-success surface: Meta
    sometimes returns 200 even when a subset of fields didn't update
    (e.g. an icebreaker with an emoji that violates a locale rule
    survives validation but is silently dropped). We re-GET after every
    push and surface the diff in this field for the portal to render.
    """

    model_config = _FORBID

    success: bool
    pushed_at: str = Field(..., description="RFC3339 UTC instant of POST completion.")
    fields_pushed: list[Literal["ice_breakers", "persistent_menu", "get_started"]]
    diff_against_remote: dict[str, object] | None = Field(
        None,
        description=(
            "Non-None when re-GET of /messenger_profile shows mismatch vs "
            "what we sent. Keys map to bundle sections; values describe "
            "the drift. Drives portal 'out-of-sync' banner."
        ),
    )
    error: str | None = Field(
        None,
        description=(
            "Classified error string when success=False. "
            "Vocabulary: 'meta_400_invalid_payload', 'meta_401_token_revoked', "
            "'meta_5xx_after_retries', 'timeout'."
        ),
    )


__all__ = [
    "MessengerGetStarted",
    "MessengerIcebreaker",
    "MessengerPersistentMenu",
    "MessengerProfileBundle",
    "MessengerProfilePushResult",
    "PersistentMenuCallToAction",
]
