"""Groq Whisper transcription client for inbound voice notes — voice-notes M1.

Pure HTTP (no DB, no SDK) — mirrors ``meta_whatsapp.py`` /
``telegram_client.py``: a thin ``httpx`` adapter over Groq's
OpenAI-compatible transcription endpoint. Avoiding the ``groq`` SDK keeps
the stub/test posture identical to the rest of the service layer and adds
no new dependency.

The endpoint auto-detects the spoken language when the ``language`` param
is omitted. With ``response_format=verbose_json`` the response includes a
``language`` field, so a single call gives us both the transcript (in the
spoken language — EN / MS / ZH) AND the detected language tag. No separate
language detector is needed: ``llm.generate_response`` already replies in
the user's language once the transcript is substituted as the message text.

Fail-safe by contract: every failure (placeholder key, HTTP error, non-JSON
body, empty transcript, network error) raises ``TranscriptionError``. The
pipeline catches it and sends the polite "please type" fallback — a bad
transcript never reaches the LLM as a confident wrong answer. The Groq API
key is never logged or embedded in a raised error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

import httpx

from polaris_api.config import get_settings

logger = logging.getLogger(__name__)

# OpenAI-compatible transcription endpoint (multipart form-data).
GROQ_TRANSCRIPTION_URL: Final[str] = (
    "https://api.groq.com/openai/v1/audio/transcriptions"
)

# Canned stub transcript (E2E / boot-without-key). Mirrors llm.py's
# _stub_envelope: deterministic, offline, no cost.
_STUB_TEXT: Final[str] = "hello this is a test"
_STUB_LANGUAGE: Final[str] = "en"


@dataclass(slots=True)
class TranscriptionResult:
    """One successful transcription.

    ``text`` is the transcript in the spoken language (already stripped,
    guaranteed non-empty). ``language`` is Groq/Whisper's auto-detected
    language tag (e.g. ``"english"``, ``"malay"``, ``"chinese"``), or
    ``"unknown"`` when the response omitted it.
    """

    text: str
    language: str


class TranscriptionError(RuntimeError):
    """Raised on any transcription failure (caller falls back gracefully).

    ``status_code`` is the HTTP status, or 0 for network / precondition
    failures.
    """

    def __init__(self, message: str, *, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


async def transcribe(
    audio: bytes, mime_type: str, *, filename: str,
) -> TranscriptionResult:
    """Transcribe ``audio`` via Groq Whisper → ``TranscriptionResult``.

    ``mime_type`` / ``filename`` describe the blob for the multipart upload
    (Groq accepts ogg, m4a, mp3, wav, webm, …). Raises ``TranscriptionError``
    on any failure; never returns an empty transcript.
    """
    settings = get_settings()

    if settings.transcription_stub_enabled:
        return TranscriptionResult(text=_STUB_TEXT, language=_STUB_LANGUAGE)

    key = settings.groq_api_key
    if not key or key.startswith("placeholder-"):
        raise TranscriptionError(
            "GROQ_API_KEY is unset or still a placeholder; refusing to call "
            "Groq. Provision a real key or enable TRANSCRIPTION_STUB_ENABLED."
        )

    headers = {"Authorization": f"Bearer {key}"}
    # Omit ``language`` → Whisper auto-detects; verbose_json surfaces it back.
    data = {
        "model": settings.transcription_model,
        "response_format": "verbose_json",
    }
    files = {"file": (filename, audio, mime_type)}

    try:
        async with httpx.AsyncClient(timeout=settings.transcription_timeout_s) as client:
            response = await client.post(
                GROQ_TRANSCRIPTION_URL, headers=headers, data=data, files=files,
            )
    except httpx.RequestError as exc:
        # httpx exceptions embed the request — re-raise WITHOUT chaining so the
        # Authorization header / key can't surface via the exception cause.
        logger.warning("transcription network error: %s", type(exc).__name__)
        raise TranscriptionError(
            f"transcription network error: {type(exc).__name__}", status_code=0
        ) from None

    if response.status_code >= 400:
        logger.warning("transcription non-2xx: status=%d", response.status_code)
        raise TranscriptionError(
            f"transcription failed: HTTP {response.status_code}",
            status_code=response.status_code,
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise TranscriptionError("transcription response was not JSON") from exc

    text = (body.get("text") or "").strip() if isinstance(body, dict) else ""
    if not text:
        # Empty transcript = silence / unintelligible audio → treat as failure
        # so the caller asks the customer to type rather than replying to "".
        raise TranscriptionError("transcription returned an empty transcript")

    language = (body.get("language") if isinstance(body, dict) else None) or "unknown"
    return TranscriptionResult(text=text, language=str(language))
