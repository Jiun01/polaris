"""AI follow-up (re-engagement) feature — Phase 13 follow-ups.

Sends one context-aware AI follow-up to a customer who went quiet inside
WhatsApp's free 24h service window, when no human handoff occurred and the
customer stopped replying — timed to a humane, tenant-local hour.

Submodules:
  * ``timing``   — pure send-time + quiet-hours logic (no I/O).
  * ``scheduler`` — schedule / cancel pending jobs in the inbound pipeline.
  * ``worker``    — tick loop that claims due jobs, generates + sends.
"""
