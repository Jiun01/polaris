"""Inbound webhook dispatcher — Phase 2.

Modules under this package handle externally-originated HTTPS callbacks
(Meta WhatsApp Cloud, Instagram DM, Facebook Messenger; Chatwoot Agent Bot).
Common flow per request:

1. Read the raw request body (`await request.body()`) — required because
   HMAC verification MUST run over the exact bytes Meta/Chatwoot signed.
2. Constant-time HMAC verify (`webhooks/_hmac.py`).
3. Resolve tenant from per-channel external ids (PR-2 +
   `services/webhook_resolver.py`).
4. Status-gate per ADR-018 / ADR-049 (PR-3 + `webhooks/_status_gate.py`).
5. Redis SETNX idempotency + normalised `IncomingMessage` enqueue (PR-4).
6. Demo short-circuit (PR-6).

Architectural rule: modules under `webhooks/` **never** import the
BYPASSRLS pool accessor. The lint at `apps/api/scripts/lint_service_pool.py`
enforces this. Tenant resolution lives in `services/webhook_resolver.py`,
which is the only Phase 2 surface that may use the BYPASSRLS pool.
"""
