"""Chatwoot outbound integration — Phase 7.

Three modules ship under this package:

  - ``client.py``     — PR-2 — outbound HTTP client (6 verbs + retry budget +
                         declined-vs-errored error split per ADR-051).
  - ``handoff.py``    — PR-3 — business-hours-aware handoff orchestration;
                         dual-write of customer message + Chatwoot calls.
                         Invoked by ``ChatwootAdapter.handoff()`` in
                         services/agent/ports.py.
  - ``label_sync.py`` — PR-4 — pol_opted_out + pol_bot_suspended label sync
                         (hooks into Phase 6 opt-out path + tenant suspension).

Inbound Chatwoot traffic (webhooks) is handled by
``apps/api/polaris_api/webhooks/chatwoot_router.py`` — that lives outside
this package because webhook handlers cross-cut all channel integrations
under one router-tree per Phase 2's convention.
"""

from polaris_api.services.chatwoot.client import (
    ChatwootClient,
    ChatwootConfigError,
    ChatwootStructuralError,
    ChatwootTransientError,
    get_chatwoot_client,
    reset_client_for_testing,
)

__all__ = [
    "ChatwootClient",
    "ChatwootConfigError",
    "ChatwootStructuralError",
    "ChatwootTransientError",
    "get_chatwoot_client",
    "reset_client_for_testing",
]
