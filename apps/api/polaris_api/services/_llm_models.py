"""Internal LLM-service types — Phase 4 PR-1.

Typed exceptions only. ``services/llm.py`` raises these so callers
(``services/pipeline.py`` in PR-4) can distinguish recoverable
provider issues from configuration bugs.

  * ``LLMConfigError`` — boot-time misconfig (env var unset, API key
    is still a "placeholder-*" sentinel). Fails fast at the first
    call rather than leaking a request to the real provider endpoint
    with junk credentials.

  * ``LLMTransientError`` — Anthropic returned a 5xx / timeout /
    rate-limit. Fall-back path swallows this and routes to OpenRouter
    per ADR-031 Amendment 2026-05-15.

  * ``LLMHardFailure`` — both Anthropic AND OpenRouter failed. The
    pipeline orchestrator catches this and writes a "sorry, we're
    having technical difficulties" message instead of crashing the
    inbound worker.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base for all LLM-service errors. Never raise directly."""


class LLMConfigError(LLMError):
    """Provider key is unset or still a `placeholder-*` sentinel.

    Raised by ``_get_anthropic_client`` / ``_get_openrouter_client``.
    The error message includes the offending env var name so the
    operator knows what to fix; it never echoes the (junk) value.
    """


class LLMTransientError(LLMError):
    """Anthropic returned a recoverable error (5xx / timeout / 429).

    ``_anthropic_call_with_fallback`` catches this and routes the
    same prompt to OpenRouter `openai/gpt-4o-mini` uncached per
    ADR-031 Amendment 2026-05-15. The exception object carries the
    original SDK exception in ``__cause__`` for log correlation.
    """


class LLMHardFailure(LLMError):
    """Both Anthropic AND OpenRouter failed.

    The pipeline orchestrator (PR-4) catches this and emits a
    fallback "we're having technical difficulties" reply, plus a
    Chatwoot private note for operator visibility. Surfacing this as
    a distinct type (not a bare ``Exception``) keeps the worker's
    error-tier metrics meaningful.
    """
