"""Polaris API config — Pydantic Settings.

Pulls env vars per docs/Polaris V2/04a_PHASE_0_DEEP.md §1.3 (Phase 0) +
04b_PHASE_1_DEEP.md §1.3 (Phase 1 additions).

Precedence policy: ``.env`` is the source of truth. OS env values are
honored only when non-empty (so tests setting `monkeypatch.setenv("X",
"placeholder-...")` still take effect). An empty-string OS env var
(common in dev shells that exported `X=""`) does NOT shadow the `.env`
value — operator preference 2026-05-18; trades the upstream
pydantic-settings default for predictable single-source-of-truth
behavior matching the user's mental model of "`.env` controls
everything; the shell doesn't sneak in."
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _scrub_empty_env_vars() -> None:
    """Delete os.environ entries whose value is the empty string.

    Empty-string OS env vars (the common dev-shell footgun:
    ``$env:X=""`` or ``export X=""``) would otherwise shadow ``.env``
    values under pydantic-settings' default precedence (OS env > .env).
    Deleting them lets pydantic-settings fall through to its own
    ``env_file=".env"`` source, which IS the desired source of truth.

    Important: only EMPTY values are scrubbed. Non-empty OS env values
    (e.g. ``monkeypatch.setenv("X", "placeholder-...")``) are preserved
    so tests + explicit operator overrides still take effect.

    No load_dotenv here — pydantic-settings reads .env directly via
    its env_file config. Doing load_dotenv() at module import time
    would inject DSN env vars globally, which surfaces in test gates
    that check ``os.environ.get("POLARIS_DATABASE_URL")`` to decide
    skip-vs-run.
    """
    for k in list(os.environ.keys()):
        if os.environ.get(k) == "":
            del os.environ[k]


_scrub_empty_env_vars()


class Settings(BaseSettings):
    """Polaris API runtime settings.

    Most vars are optional at the type level so the service can boot in local
    dev with placeholder values from `.env.example`. Production guard rails
    (Phase 1 PR-3's KMS sentinel, the lifespan startup check, etc.) catch
    misconfigurations at boot rather than per-request.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @model_validator(mode="before")
    @classmethod
    def _blank_bool_uses_default(cls, data: Any) -> Any:
        """Treat a blank `.env` value for a bool flag as "use the default".

        `.env` carries some flags as a bare ``FLAG=`` (empty string) — e.g.
        ``LLM_STUB_ENABLED=`` — to mean "leave at the declared default".
        pydantic's bool parser rejects an empty string, which crashes
        ``Settings`` import. ``_scrub_empty_env_vars()`` only clears blank OS
        env vars, not blanks that live in the ``.env`` file itself. So for any
        bool-typed field whose incoming value is a blank/whitespace string,
        drop it here and let the field default apply (False for the stub
        flags, True for the opt-out flags).
        """
        if isinstance(data, dict):
            bool_fields = {
                name
                for name, field in cls.model_fields.items()
                if field.annotation is bool
            }
            for key in list(data.keys()):
                if key.lower() in bool_fields:
                    value = data[key]
                    if isinstance(value, str) and not value.strip():
                        data.pop(key, None)
        return data

    # --- Supabase ---
    supabase_url: str = Field(default="https://placeholder.supabase.co")
    supabase_anon_key: str = Field(default="placeholder-anon-key")
    supabase_service_key: str = Field(default="placeholder-service-key")

    # --- Supabase Storage buckets (Phase 9 PR-7/8/9) ---
    invoice_pdf_bucket: str = Field(default="polaris-invoices")
    supabase_storage_quotes_bucket: str = Field(default="polaris-sales-quotes")

    # --- Postgres connection strings (Phase 1 PR-2) ---
    # Two pools per ADR-002. anon = polaris_anon role (NOBYPASSRLS), used by
    # request-scoped handlers via TenantContext. service = polaris_service role
    # (BYPASSRLS), reserved for cron + admin paths (CI lint enforces).
    # Optional at the type level so local dev can boot before PR-2's migration
    # 010 has been applied + roles password-bootstrapped; routes that depend
    # on the pools raise at request time if the DSN is missing.
    polaris_db_url_anon: str = Field(default="")
    polaris_db_url_service: str = Field(default="")

    # Bounded retry around init_pools() at startup (main.py lifespan). A
    # transient Supavisor blip during deploy — the pooler answers TCP but
    # returns {:error, :econnrefused} for the upstream DB while it restarts /
    # its cache lags — would otherwise raise out of the lifespan and crash-loop
    # the container. We retry with exponential backoff (base * 2**(n-1)) so a
    # momentary outage self-heals; a sustained outage still fails loudly once
    # attempts are exhausted. Set attempts=1 to restore fail-fast behavior.
    db_pool_init_attempts: int = Field(default=5, ge=1, le=20)
    db_pool_init_base_delay_s: float = Field(default=1.0, gt=0.0, le=30.0)

    # --- Redis ---
    redis_url: str = Field(default="redis://localhost:6379")
    redis_password: str = Field(default="")

    # --- Crypto ---
    # 32 bytes hex-encoded (64 hex chars). Phase 0 may be unset; Phase 1 requires it.
    polaris_kms_key: str = Field(default="")

    # --- JWT (Phase 1 PR-2 — TenantContext) ---
    # HS256 shared secret between portal (JWT issuer) and API (JWT verifier).
    # At GA the portal issues post-magic-link tokens carrying tenant_id + user_id
    # + role claims. RS256/JWKS is a v2.1 upgrade when we add API key auth and
    # cross-service signing.
    jwt_secret: str = Field(default="")
    jwt_algorithm: Literal["HS256"] = Field(default="HS256")
    jwt_issuer: str = Field(default="polaris-portal")
    jwt_leeway_seconds: int = Field(default=10)

    # --- Portal magic-link issuance (Phase 9 PR-3) ---
    # When the admin app creates a tenant via /api/admin/tenants, the API
    # mints a portal magic-link compatible with the portal's NextAuth
    # EmailProvider (consumes via apps/portal/lib/auth/adapter.ts).
    # Token hashing must mirror NextAuth v4's convention: sha256(token +
    # portal_nextauth_secret).hexdigest(). The unhashed token is mailed
    # in the URL; the hashed value lands in magic_link_tokens.
    #
    # When portal_nextauth_secret OR portal_invite_smtp_host is empty,
    # the dispatch silently no-ops (logged, audit-recorded) and the
    # admin must direct the new user to portal.getpolaris.my/login
    # manually. This keeps dev environments without SMTP usable.
    portal_nextauth_secret: str = Field(default="")
    portal_base_url: str = Field(default="https://portal.getpolaris.my")
    # Public base URL of THIS api service — used to build the Telegram webhook
    # URL at provisioning (setWebhook). Off-Meta channels M2.
    public_api_base_url: str = Field(default="https://api.getpolaris.my")
    portal_invite_expiry_minutes: int = Field(default=15, ge=1, le=60)
    portal_invite_smtp_host: str = Field(default="")
    portal_invite_smtp_port: int = Field(default=587, ge=1, le=65535)
    portal_invite_smtp_user: str = Field(default="")
    portal_invite_smtp_password: str = Field(default="")
    portal_invite_smtp_from: str = Field(
        default="Polaris <no-reply@getpolaris.my>"
    )
    portal_invite_smtp_timeout_s: float = Field(default=10.0, gt=0.0, le=60.0)

    # --- Meta (demo WABA placeholders; consumed Phase 2+) ---
    # meta_app_secret verifies HMAC on the WhatsApp + Messenger webhooks that the
    # Chatwoot fork RELAYS to Polaris (wa-status / meta-template-status /
    # wa-account routers). Stays default="" so local dev can boot;
    # verify_meta_signature fails closed on empty secret (no silent bypass via
    # "empty signs empty"). NOTE: post Chatwoot-native rehaul the old meta_router
    # inbound path is deleted, so meta_webhook_verify_token + meta_instagram_app_secret
    # (legacy two-secret IG verify) are gone — inbound is HMAC'd via
    # chatwoot_webhook_hmac_secret instead.
    meta_app_secret: str = Field(default="")
    # Meta Graph API version pin. v25.0 is the current public version
    # (released 2026-02-18, https://developers.facebook.com/blog/post/2026/02/18/introducing-graph-api-v25-and-marketing-api-v25/).
    # Override per environment if Meta deprecates v25 before our next rebase.
    meta_graph_api_version: str = Field(default="v25.0")
    # (ADR-066 removed the shared demo-WABA env vars META_DEMO_PHONE_NUMBER_ID /
    # _WABA_ID / _WABA_ACCESS_TOKEN — demo dispatch now uses the gateway
    # channel's own per-tenant creds, resolved by channel owner.)
    # E2E: redirect Meta Graph API calls to a local capture router.
    # Set to http://localhost:8000/meta-mock (or http://polaris-api:8000/meta-mock
    # in Docker). When empty, uses the real Graph API base URL.
    meta_api_base_url: str = Field(default="https://graph.facebook.com")
    # Instagram-API-with-Instagram-Login sends via graph.instagram.com (NOT the
    # graph.facebook.com Messenger edge). Separate base URL so E2E can redirect
    # it to the local capture router independently of meta_api_base_url.
    meta_instagram_api_base_url: str = Field(default="https://graph.instagram.com")
    # E2E: mount the /meta-mock capture router. Must be True when meta_api_base_url
    # points to this process; safe to leave False in staging/prod (meta_api_base_url
    # defaults to the real Graph API anyway).
    meta_mock_enabled: bool = Field(default=False)

    # --- Chatwoot (Phase 2 PR-5) ---
    # Shared secret used by the Chatwoot fork (v4.13.0+) to sign outbound
    # Agent-Bot webhook deliveries. One secret per Chatwoot install at GA;
    # per-inbox secret rotation is Phase 8 portal work (Q-PHASE2-01).
    chatwoot_webhook_hmac_secret: str = Field(default="")

    # --- Chatwoot internal token provisioning ---
    # Bearer token for the Polaris-internal fork endpoint that vends per-inbox
    # channel tokens (GET /internal/inboxes/{id}/token). Distinct from the
    # public Chatwoot API token — this endpoint is only reachable within the
    # cluster and is gated by X-Polaris-Internal-Token. Empty = feature
    # disabled (get_channel_token returns None without making any HTTP call).
    chatwoot_internal_token: str = Field(default="")
    # In-cluster base URL for the Chatwoot Rails container. Used only for the
    # /internal/* provisioning endpoints so calls bypass the public Caddy
    # proxy. Override via CHATWOOT_INTERNAL_BASE_URL in .env.
    chatwoot_internal_base_url: str = Field(default="http://chatwoot-rails:3000")

    # --- Chatwoot outbound (Phase 7 PR-2) ---
    # Single platform URL per Q-PHASE7-07 (ADR-024 — Polaris hosts one
    # Chatwoot install at GA; per-tenant scoping via tenants.chatwoot_account_id
    # baked into the URL). Self-hosted-per-tenant Chatwoot is v3.0 scope.
    chatwoot_base_url: str = Field(default="https://chat.getpolaris.my")

    # Platform-wide Chatwoot agent_bots global token per Q-PHASE7-01.
    # Chatwoot's agent_bots issue a SINGLE token that attaches across all
    # chatwoot_account_id values via the super-admin agent_bots table —
    # this is why one token works across all Polaris tenants. The client
    # composes URLs with the per-tenant account_id; the token is fixed.
    # Defaults to "placeholder-..." so local dev can boot before operator
    # pastes the real value; ChatwootClient raises ChatwootConfigError on
    # any non-test call attempt with the placeholder value.
    chatwoot_bot_access_token: str = Field(default="placeholder-chatwoot-bot-token")

    # User-level API token for CW endpoints that reject bot tokens (contacts,
    # conversations create).  CW returns 401 "Access to this endpoint is not
    # authorized for bots" for those routes; a regular agent/admin token from
    # /auth/sign_in is required.  Empty = feature disabled (pipeline skips CW
    # conversation linking silently).
    chatwoot_agent_api_token: str = Field(default="")

    # Outbound HTTP client tuning per 04h §1.3.
    chatwoot_api_timeout_s: float = Field(default=8.0, gt=0.0, le=60.0)
    chatwoot_api_max_retries: int = Field(default=2, ge=0, le=5)

    # Q-PHASE7-02 hybrid label provisioning. When True, services/chatwoot/client.py
    # calls ensure_label() before every apply. Phase 9 tenant provisioning ALSO
    # seeds pol_opted_out + pol_bot_suspended at tenant creation; this is the
    # defence-in-depth in case a label was deleted out-of-band.
    chatwoot_label_auto_provision: bool = Field(default=True)

    # --- Messenger Profile push (Phase 7 PR-5) ---
    messenger_profile_api_timeout_s: float = Field(default=10.0, gt=0.0, le=60.0)

    # --- Handoff (Phase 7 PR-3) ---
    # Cap on the rendered private note (after {{last_reply}} substitution)
    # before truncation. Chatwoot's hard limit is 8k; 4000 leaves headroom
    # for the template wrapper text + future extension.
    handoff_private_note_max_len: int = Field(default=4000, gt=0, le=8000)

    # Strict business-hours cutoff at GA per Q-PHASE7-03. Minutes added
    # to business_hours.close (and subtracted from open) before classifying
    # an inbound as after-hours. 0 = strict — the configured close minute
    # is the last in-hours second. Lever exists for v2.1 if tenants ask.
    handoff_business_hours_grace_min: int = Field(default=0, ge=0, le=120)

    # Posted as a Chatwoot private note on the NEXT inbound after a
    # partial-failure handoff per Q-PHASE7-04. ``{ts}`` and ``{reason}``
    # tokens are substituted at format time (services/chatwoot/handoff.py).
    handoff_recovery_note_template: str = Field(
        default=(
            "(handoff requested at {ts} but the messaging inbox was unreachable: "
            "{reason} — agent please review)"
        )
    )

    # --- EA fork (Phase 7 PR-6) ---
    # Bearer-JWT client against ghcr.io/jiun01/polaris-ea:v1.6.0-r2.
    # Per-call short-lived JWT with audience='ea-fork'; EA server-side
    # enforces tenant scope via the tenant_id claim (ADR-024).
    ea_api_base_url: str = Field(default="https://calendar.getpolaris.my")
    # Public, customer-facing booking URL for ALL customer-visible links (bot
    # replies, redirect route). Distinct from ea_api_base_url which is
    # overridden to http://ea-nginx:80 in prod for internal EA API calls.
    # Set EA_BOOKING_PUBLIC_URL in .env / compose to override; default is
    # correct for production so missing the env var is not a bug.
    ea_booking_public_url: str = Field(default="https://calendar.getpolaris.my")
    # Public, customer-facing booking host used ONLY by the admin health probe
    # (QA finding H1). ``ea_api_base_url`` is overridden in prod to the internal
    # ``http://ea-nginx:80`` for booking API calls — but that internal nginx
    # answers 200 on ``/`` even when the PHP/booking origin is 502-ing, so a
    # probe against it is false-green. This URL goes through Cloudflare → Caddy
    # → nginx → php so the tile reflects what a real customer sees.
    ea_health_url: str = Field(default="https://calendar.getpolaris.my/")
    ea_jwt_signing_secret: str = Field(default="placeholder-ea-jwt-secret")
    ea_jwt_audience: str = Field(default="ea-fork")
    ea_jwt_issuer: str = Field(default="polaris")
    # 60s caps replay surface tightly — EA fork is co-located in the
    # same VPC at GA, bot call latency is ~2-3s, so a token valid
    # longer than ~1 minute is more replay surface than the transport
    # warrants. _mint_jwt fires per-request anyway, so there's no UX
    # cost to a short TTL. (security-reviewer Nit 1.) Bump to 300+ if
    # adding async/queued booking flows post-GA.
    ea_jwt_ttl_seconds: int = Field(default=60, ge=30, le=3600)
    ea_api_timeout_s: float = Field(default=8.0, gt=0.0, le=60.0)
    # Default 0 — booking is interactive (bot conversation), retries
    # hurt UX more than they help. Caller (booking flow node) decides.
    ea_api_max_retries: int = Field(default=0, ge=0, le=5)

    # --- Booking-via-EA-link binding (v2.1 / PR-1) ---
    # HMAC key the booking node signs the ``polaris_ref`` binding token
    # with; the EA inbound webhook verifies it. Empty / "placeholder-"
    # fails closed (binding_token.mint/verify raise) so an unverifiable
    # token is never issued or accepted. Distinct from jwt_secret so a
    # portal-JWT rotation doesn't invalidate in-flight booking links.
    # "placeholder-" default matches the platform convention (mint/verify
    # fail closed on a placeholder- or empty value, so a real token is
    # never issued with an unusable key).
    booking_binding_secret: str = Field(default="placeholder-booking-binding-secret")
    # Lifetime of a booking link before the pending row is swept. 1h is
    # generous for a customer to finish picking a slot on EA's web UI.
    booking_binding_ttl_seconds: int = Field(default=3600, ge=300, le=86_400)
    # Shared secret the polaris-ea-fork signs its appointment webhook body
    # with (X-Polaris-Signature: sha256=<hexdigest>). Placeholder/empty
    # fails closed (401).
    ea_webhook_hmac_secret: str = Field(default="placeholder-ea-webhook-hmac-secret")
    # Shared secret Polaris signs the internal EA provisioning calls with
    # (POST /api/v1/internal/tenants/provision etc.; X-Polaris-Signature:
    # sha256=<hexdigest> over the raw body). MUST equal the EA fork's
    # POLARIS_WEBHOOK_SECRET (config/polaris.php -> polaris_webhook_secret).
    # Placeholder/empty fails closed before any HTTP (EABookingConfigError).
    ea_provisioning_hmac_secret: str = Field(
        default="placeholder-ea-provisioning-hmac-secret"
    )

    # --- LLM (Phase 4 PR-1 — services/llm.py) ---
    # All three provider keys default to "placeholder-*" sentinels so local
    # dev can boot before the operator pastes real keys into `.env`. The
    # `_get_anthropic_client` / `_get_openrouter_client` / `_get_cohere_client`
    # factories raise `LLMConfigError` if the env value starts with
    # "placeholder-" — that fails closed instead of leaking a request to
    # the real provider endpoint with a junk key.
    anthropic_api_key: str = Field(default="placeholder-anthropic-key")
    # E2E: deterministic LLM stub. When true, classify_intent + generate_response
    # return canned values (no API call) AND the inbound pipeline worker starts
    # even without a real Anthropic key. Inert/False in staging+prod.
    llm_stub_enabled: bool = Field(default=False)
    # E2E: deterministic cost stub. When true, the admin cost endpoints return
    # synthetic, month-aware, band-spread (green/amber/red) figures for the real
    # fixture tenants instead of Grafana-Mimir zeros — so the /cost specs can run
    # for real without a live Mimir. Future months still resolve 'unavailable'
    # (drives the empty-state branch). Inert/False in staging+prod.
    cost_stub_enabled: bool = Field(default=False)
    openrouter_api_key: str = Field(default="placeholder-openrouter-key")
    cohere_api_key: str = Field(default="placeholder-cohere-key")

    # Model IDs — overridable so a Claude version bump becomes an env-var
    # tweak + a `claude-api` skill audit, not a code change. Defaults
    # match `.env.example` (Stage 0 of Phase 4).
    llm_haiku_model: str = Field(default="claude-haiku-4-5-20251001")
    llm_sonnet_model: str = Field(default="claude-sonnet-5")

    # Per-request timeout for the Anthropic client (QA finding L6). The SDK
    # default is 600s, so a single degraded call could block a reply for ~10
    # min; one QA reply took 19.5s. Cap it so a hang fails fast and falls back
    # to OpenRouter (``APITimeoutError`` is already a transient-fallback error).
    llm_request_timeout_s: float = Field(default=20.0, gt=0.0, le=120.0)

    # Max-tokens routing per ADR-031 Amendment 2026-05-15.
    llm_max_tokens_simple: int = Field(default=500, ge=1, le=4096)
    llm_max_tokens_medium: int = Field(default=800, ge=1, le=4096)
    llm_max_tokens_hard: int = Field(default=1000, ge=1, le=8192)

    # Prompt-cache TTL on the system block. "5m" is the Anthropic
    # default; "1h" is also supported. Phase 4 ships "5m"; revisit
    # after PR-6 demo telemetry shows hit rate (deep-dive §11
    # Q-PHASE4-02).
    llm_cache_ttl: Literal["5m", "1h"] = Field(default="5m")

    # Conversation history window per ADR-031 — load_history trims to
    # the FIRST limit hit. Both are tenant-overridable via the Phase 8
    # portal later; these are the floor defaults.
    conversation_history_max_messages: int = Field(default=20, ge=1, le=200)
    conversation_history_max_tokens: int = Field(default=8000, ge=100, le=100_000)

    # Phase 4 PR-6 — query-side embedding (resolves Q-PHASE4-05).
    # Matches the desktop ingestion app's default
    # (Polaris Ingestion System V2/polaris_ingest/app/utils/embeddings.py)
    # so query + ingest vectors land in the same embedding space. MRL
    # truncation to 1024 keeps parity with knowledge_chunks.embedding
    # VECTOR(1024) from migration 020. Model locked by ADR-059
    # (qwen/qwen3-embedding-8b). Change this var + the desktop's matching
    # setting together — drift produces silently-wrong cosine scores.
    embedding_model: str = Field(default="qwen/qwen3-embedding-8b")

    # --- Inbound voice notes (voice-notes M1) ---
    # Groq Whisper transcription for inbound voice notes. Gated to Pro+
    # tenants (``voice_note_tiers``); Starter customers get a polite
    # "please type" reply instead of a transcript. groq_api_key defaults
    # to "placeholder-*" so local dev boots before the operator pastes a
    # real key; services/transcription.py fails closed (raises
    # TranscriptionError) on a placeholder/empty key rather than calling
    # Groq with junk — same posture as the LLM provider factories above.
    groq_api_key: str = Field(default="placeholder-groq-key")
    # whisper-large-v3 (full, not -turbo) — best multilingual accuracy for
    # the EN / MS / ZH launch set per the PRD. Override per env if Groq
    # deprecates the model id.
    transcription_model: str = Field(default="whisper-large-v3")
    # Master switch. False = skip transcription entirely (voice notes fall
    # through to the media-only handoff path). True at GA.
    transcription_enabled: bool = Field(default=True)
    # E2E: deterministic transcription stub. When true, services/
    # transcription.py returns a canned ("hello this is a test", "en")
    # result WITHOUT calling Groq AND the pipeline can boot without a real
    # GROQ_API_KEY. Mirrors llm_stub_enabled. Inert/False in staging+prod.
    transcription_stub_enabled: bool = Field(default=False)
    # Per-request HTTP timeout for the Groq transcription call. Voice notes
    # are short (billed min 10s) so a transcript returns in ~1-3s; 15s
    # leaves headroom without blocking the single in-process worker for long.
    transcription_timeout_s: float = Field(default=15.0, gt=0.0, le=60.0)
    # Defensive cap on a downloaded audio blob (bytes). Groq's free-tier
    # limit is 25MB; voice notes are far smaller. Guards against a
    # malicious/oversized attachment OOMing the worker.
    transcription_max_download_bytes: int = Field(
        default=25 * 1024 * 1024, gt=0, le=100 * 1024 * 1024
    )
    # Plan tiers permitted to use voice-note transcription. Starter is
    # excluded (Pro+ only per the PRD). Frozenset gate mirrors
    # services/rag.py::_RERANK_DEFAULT_TIERS — no migration / per-tenant row.
    voice_note_tiers: frozenset[str] = Field(
        default=frozenset({"pro", "ultra", "enterprise"})
    )

    # --- Knowledge Gap Detection (M1 — miss capture) ---
    # Master switch. False = skip miss capture entirely (the bot still replies
    # exactly as before; nothing is logged). True at GA — capture is fail-soft.
    knowledge_gap_capture_enabled: bool = Field(default=True)
    # Knowledge Gap Detection is an ULTRA-tier feature: both capture AND the
    # portal dashboard are gated to these plan tiers. A non-Ultra tenant logs
    # nothing (no rows, no PII) and sees no screen. Frozenset gate mirrors
    # ``voice_note_tiers`` — no migration / per-tenant row.
    knowledge_gap_tiers: frozenset[str] = Field(
        default=frozenset({"ultra", "enterprise"})
    )
    # rrf_score floor below which a generated reply counts as a 'low_retrieval'
    # miss (higher rrf_score = better match; see knowledge_chunk.py). Default 0.0
    # = low_retrieval DISABLED — the score's scale differs between the hybrid-RRF
    # and vector-only-fallback paths, so a single threshold can't be trusted
    # until tuned on real conversation data (PRD open question). At GA the live
    # signals are the unambiguous 'no_retrieval' (zero chunks) + 'handoff'; raise
    # this above 0.0 once a defensible floor is measured.
    knowledge_gap_retrieval_threshold: float = Field(default=0.0, ge=0.0)
    # Cosine-SIMILARITY floor (0..1, higher = closer) below which a reply counts
    # as a 'low_retrieval' miss and (after 2 consecutive misses) triggers the
    # opt-in human-escalation offer — report F2/F3, migration 113.
    #
    # Why cosine, not rrf_score: per /search-first 2026-06-14 (lit. review +
    # Qwen3-Embedding model card + Cohere/Pinecone docs), RRF output is a
    # rank-fusion artifact (~1/60 scale), NOT interpretable relevance — you must
    # gate on raw cosine from the vector arm. pgvector `<=>` is magnitude-
    # invariant, so 1-distance is true cosine similarity regardless of provider
    # normalization.
    #
    # Default 0.70: GROUNDED, not guessed. The generic "0.5" cited in the
    # literature assumes Qwen3's `Instruct:` query prefix; services/embed.py uses
    # bare text (consistently on both index + query side), which shifts the
    # distribution, so the model-card numbers don't transfer. Measured on the
    # ACTUAL pipeline (1024-dim, bare-text, pgvector cosine): real hits 0.76-0.85,
    # no-matches 0.54-0.69 → 0.70 separates them. This is a SMALL synthetic
    # sample: re-calibrate with an F1 sweep on the existing eval harness
    # (`evals/rag_queries.yaml` + `benchmarks/run.py`, extended with no-match
    # cases) and re-tune on real traffic. Upgrade path: gate on a cross-encoder
    # reranker score (bge-reranker-v2-m3 self-host) for a more robust signal.
    # Set to 0.0 to DISABLE gap logging + the escalation offer.
    knowledge_gap_min_similarity: float = Field(default=0.70, ge=0.0, le=1.0)

    # --- audit Batch B (B2): opt-in RAG skip for `simple`-tier messages ---
    # Default OFF: greetings/small-talk still run the full hybrid RAG search
    # exactly as before. When enabled, `simple`-tier messages (the classic
    # pipeline path) skip the embed call + rag.search_hybrid entirely — the
    # catalogue/promotions lookups are already unconditionally gated on
    # intent != "simple" (no flag needed for those). Opt-in because it's a
    # visible behavior change: a simple greeting won't retrieve any KB
    # context even if a chunk would technically match the text.
    simple_tier_skip_rag: bool = Field(default=False)

    # --- Phase 9 PR-11: Admin email (weekly digest + alerts) ---
    admin_email_server_host: str = Field(default="")
    admin_email_server_port: int = Field(default=587, ge=1, le=65535)
    admin_email_server_user: str = Field(default="")
    admin_email_server_password: str = Field(default="")
    admin_email_from: str = Field(default="")
    admin_email_to: str = Field(default="")

    # --- Observability (Phase 10 PR-2 — ADR-016 / ADR-035) ---
    # All four default to empty so local dev boots without any cloud credentials.
    # Sentry is disabled when sentry_dsn is empty; OTel is disabled when
    # otel_endpoint is empty.  Neither absence is treated as an error — the
    # cost dashboard and error alerting simply don't function until the ops team
    # pastes the real values (see infra/grafana-dashboards/ README + ../docs/internal-ops/RUNBOOK.md).
    sentry_dsn: str = Field(default="")
    # Grafana Cloud OTLP gateway — copy from Grafana Cloud → Connections →
    # OpenTelemetry.  Format: https://otlp-gateway-<region>.grafana.net/otlp
    otel_endpoint: str = Field(default="")
    otel_service_name: str = Field(default="polaris-api")
    # Comma-separated "key=value" auth headers for the OTLP exporter.
    # Grafana Cloud: "Authorization=Basic <base64(instanceId:apiKey)>"
    # Leave empty for unauthenticated local collectors.
    otel_headers: str = Field(default="")
    # Human-facing dashboard deep-links for the System Health "Recent failures"
    # tile (optional). These are the browser URLs an operator clicks to reach
    # the full logs/traces — distinct from the OTLP ingest endpoint / Sentry DSN
    # above. Empty = the tile renders no link.
    grafana_dashboard_url: str = Field(default="")
    sentry_project_url: str = Field(default="")
    # Sentry API credentials for the System Health APM tile.
    # sentry_auth_token: user-auth token with project:read scope.
    # sentry_org_slug / sentry_project_slug: from your Sentry project URL.
    # sentry_api_base: override for self-hosted Sentry installs.
    # All four default to empty; the APM tile shows configured=False when
    # sentry_auth_token is empty (no error raised).
    sentry_auth_token: str = Field(default="")
    sentry_org_slug: str = Field(default="")
    sentry_project_slug: str = Field(default="")
    sentry_api_base: str = Field(default="https://sentry.io")

    # --- Phase 10 PR-4: Cost ceiling + platform spend alert ---
    # Monthly platform-wide LLM spend threshold in MYR.  0 = disabled.
    # When all-tenant combined cost for the current month exceeds this
    # value, a Sentry fatal alert fires.  Default 0 (off at GA — operator
    # sets a sensible ceiling once real billing data arrives from Mimir).
    platform_daily_spend_threshold_myr: float = Field(default=0.0, ge=0.0)

    # Grafana Cloud Mimir URL + token (also read by admin_cost.py).
    # Declared here so cost_ceiling.py can read them without going through
    # the router layer.  Defaults to empty so local dev skips all Mimir calls.
    grafana_cloud_mimir_url: str = Field(default="")
    grafana_cloud_mimir_token: str = Field(default="")

    # USD→MYR conversion for the per-tenant cost dashboard. Provider LLM /
    # embedding prices are quoted in USD on their price pages; grafana_cost.py
    # converts to ringgit through this single rate. It's a manual estimate, not
    # a live FX feed — bump it (env USD_TO_MYR_RATE) when the rate drifts
    # materially. Default ~4.15.
    usd_to_myr_rate: float = Field(default=4.15, gt=0.0)

    # --- Ops ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    environment: Literal["local", "staging", "prod", "production"] = Field(default="local")

    # --- E2E testing (local VPS-mimic / CI only) ---
    # When non-empty, mounts the /internal/e2e/* seed router. Never set in staging/prod.
    polaris_e2e_internal_secret: str = Field(default="")

    # --- Version (commit SHA short; set by container at build time) ---
    version: str = Field(default="dev")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Lazy singleton — avoids reading env at import time (test-friendly).

    `lru_cache` makes this thread-safe under the GIL; the previous
    check-then-assign pattern had a race where two coroutines/threads
    could each construct a `Settings()` and the second would stomp the
    first silently. Tests that need a fresh instance call
    `get_settings.cache_clear()`.
    """
    return Settings()
