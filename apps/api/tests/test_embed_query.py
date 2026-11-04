"""Query-side embedding tests — Phase 4 PR-6.

Covers ``services/embed.embed_query``:
  - happy path returns 1024-dim list from OpenRouter
  - placeholder key → zero vector + one-time warning
  - empty input → zero vector (skip the API call)
  - API error → zero vector (fails soft so RAG still serves BM25)
  - dim mismatch → zero vector (defensive against EMBEDDING_MODEL drift)
  - MRL parameter only sent to MRL-supported models
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import openai
import pytest

pytestmark = pytest.mark.asyncio


def _mock_embed_response(*, dim: int = 1024) -> MagicMock:
    """Build a MagicMock that quacks like ``openai.types.CreateEmbeddingResponse``."""
    response = MagicMock()
    data_item = MagicMock()
    data_item.embedding = [0.5] * dim
    response.data = [data_item]
    return response


@pytest.fixture
def mock_embed_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Install a mock OpenRouter embeddings client + reset cache + warning flag."""
    from polaris_api.services import embed as embed_module

    embed_module._reset_client_for_testing()
    embed_module.reset_warning_for_testing()

    mock = MagicMock()
    mock.embeddings.create = AsyncMock(return_value=_mock_embed_response())
    monkeypatch.setattr(embed_module, "_get_embed_client", lambda: mock)
    return mock


async def test_happy_path_returns_1024_dim_vector(
    mock_embed_client: MagicMock,
) -> None:
    from polaris_api.services import embed

    vec = await embed.embed_query("what time you open")

    assert len(vec) == 1024
    assert all(isinstance(v, float) for v in vec)
    mock_embed_client.embeddings.create.assert_awaited_once()


async def test_empty_input_returns_zero_vector_no_api_call(
    mock_embed_client: MagicMock,
) -> None:
    """Empty / whitespace-only text → zero vector, no OpenRouter call."""
    from polaris_api.services import embed

    vec = await embed.embed_query("   ")

    assert vec == [0.0] * 1024
    mock_embed_client.embeddings.create.assert_not_called()


async def test_placeholder_key_returns_zero_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Placeholder OPENROUTER_API_KEY → zero vector, no SDK construction."""
    from polaris_api.config import get_settings
    from polaris_api.services import embed

    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder-openrouter-key")
    get_settings.cache_clear()
    embed._reset_client_for_testing()
    embed.reset_warning_for_testing()

    vec = await embed.embed_query("hello")

    assert vec == [0.0] * 1024


async def test_api_error_returns_zero_vector(
    mock_embed_client: MagicMock,
) -> None:
    """OpenRouter 5xx → zero vector + log warning, no crash."""
    from polaris_api.services import embed

    response = MagicMock(status_code=503)
    mock_embed_client.embeddings.create.side_effect = openai.APIError(
        "upstream 503", request=MagicMock(), body=None,
    )
    _ = response  # silence linter

    vec = await embed.embed_query("hello")

    assert vec == [0.0] * 1024


async def test_dim_mismatch_returns_zero_vector(
    mock_embed_client: MagicMock,
) -> None:
    """Wrong-dim response from model → zero vector (don't poison the search)."""
    from polaris_api.services import embed

    mock_embed_client.embeddings.create.return_value = _mock_embed_response(dim=1536)

    vec = await embed.embed_query("hello")

    assert vec == [0.0] * 1024


async def test_mrl_dimensions_param_sent_for_mrl_models(
    mock_embed_client: MagicMock,
) -> None:
    """qwen/qwen3-embedding-8b is MRL → dimensions=1024 included in API call."""
    from polaris_api.services import embed

    await embed.embed_query("hi")

    kwargs = mock_embed_client.embeddings.create.call_args.kwargs
    assert kwargs["model"] == "qwen/qwen3-embedding-8b"
    assert kwargs.get("dimensions") == 1024
    assert kwargs["input"] == "hi"


async def test_mrl_param_omitted_for_non_mrl_model(
    monkeypatch: pytest.MonkeyPatch, mock_embed_client: MagicMock,
) -> None:
    """Switch EMBEDDING_MODEL to a non-MRL model → dimensions kwarg dropped."""
    from polaris_api.config import get_settings
    from polaris_api.services import embed

    monkeypatch.setenv("EMBEDDING_MODEL", "cohere/embed-multilingual-v3.0")
    get_settings.cache_clear()
    # Set the model BEFORE the client uses settings — fixture monkeypatched
    # _get_embed_client already; we just need new settings to flow through
    # embed_query's read.

    await embed.embed_query("hi")

    kwargs = mock_embed_client.embeddings.create.call_args.kwargs
    assert kwargs["model"] == "cohere/embed-multilingual-v3.0"
    assert "dimensions" not in kwargs


async def test_placeholder_warning_fires_only_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """One-shot warning so log doesn't spam under burst.

    Per PR-7 M-2 fix: `_log_placeholder_warning_once` uses
    `@lru_cache(maxsize=1)` so the second call returns the cached
    None without re-invoking the body — hence the WARNING is
    emitted exactly once. Assert by counting log records.
    """
    import logging

    from polaris_api.config import get_settings
    from polaris_api.services import embed

    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder-openrouter-key")
    get_settings.cache_clear()
    embed._reset_client_for_testing()
    embed.reset_warning_for_testing()

    with caplog.at_level(logging.WARNING, logger="polaris_api.services.embed"):
        await embed.embed_query("hello")
        await embed.embed_query("again")
        await embed.embed_query("and again")

    placeholder_warnings = [
        r for r in caplog.records
        if "OPENROUTER_API_KEY placeholder" in r.getMessage()
    ]
    assert len(placeholder_warnings) == 1, (
        f"expected exactly 1 placeholder warning, got {len(placeholder_warnings)}"
    )
