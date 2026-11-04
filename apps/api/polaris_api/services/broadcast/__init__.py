"""Broadcast services — Phase 6 PR-5 + PR-6 + PR-7.

Three modules:

  * ``service``     — PR-5. Broadcast CRUD + lifecycle (create / schedule
                      / pause / resume / cancel). Tier-gate + quota
                      gate live here.

  * ``worker``      — PR-6. Long-running drain loop. Per-recipient send
                      via meta_whatsapp + compliance gate.

  * ``compliance``  — PR-6. STOP-keyword + opted_out + 24h-window
                      check; SAME function as inbound_worker uses.

  * ``delivery``    — PR-7. wa-status webhook -> broadcast_recipients
                      forward-only state machine.

Phase 6 broadcast is Pro+ only per ADR-026 Amendment 3rd — the
tier-gate enforcement lives in ``service.create_broadcast`` (HTTP
403 ``feature_not_in_tier`` for Starter).
"""
