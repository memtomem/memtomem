"""Comprehensive tests for memtomem embedding providers.

Covers:
  - EmbeddingProvider protocol conformance
  - OllamaEmbedder (mocked httpx)
  - OpenAIEmbedder (mocked httpx)
  - OnnxEmbedder (mocked fastembed)
  - create_embedder factory
  - with_retry decorator & _parse_retry_after helper
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from memtomem.config import EmbeddingConfig
from memtomem.embedding.factory import create_embedder
from memtomem.embedding.noop import NoopEmbedder
from memtomem.embedding.ollama import OllamaEmbedder
from memtomem.embedding.onnx import OnnxEmbedder
from memtomem.embedding.openai import OpenAIEmbedder
from memtomem.embedding.retry import _parse_retry_after, with_retry
from memtomem.errors import ConfigError, EmbeddingError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ollama_config(**overrides) -> EmbeddingConfig:
    defaults = dict(
        provider="ollama",
        model="nomic-embed-text",
        dimension=768,
        base_url="http://localhost:11434",
        api_key="",
        batch_size=64,
        max_concurrent_batches=4,
    )
    defaults.update(overrides)
    return EmbeddingConfig(**defaults)


def _onnx_config(**overrides) -> EmbeddingConfig:
    defaults = dict(
        provider="onnx",
        model="all-MiniLM-L6-v2",
        dimension=384,
        base_url="",
        api_key="",
        batch_size=64,
        max_concurrent_batches=4,
    )
    defaults.update(overrides)
    return EmbeddingConfig(**defaults)


def _openai_config(**overrides) -> EmbeddingConfig:
    defaults = dict(
        provider="openai",
        model="text-embedding-3-small",
        dimension=1536,
        base_url="https://api.openai.com",
        api_key="sk-test-key",
        batch_size=64,
        max_concurrent_batches=4,
    )
    defaults.update(overrides)
    return EmbeddingConfig(**defaults)


def _make_httpx_response(
    status_code: int = 200, json_data: dict | None = None, headers: dict | None = None
) -> httpx.Response:
    """Build a minimal httpx.Response for mocking."""
    resp = httpx.Response(
        status_code=status_code,
        json=json_data,
        headers=headers or {},
        request=httpx.Request("POST", "http://test"),
    )
    return resp


# ---------------------------------------------------------------------------
# 1. EmbeddingProvider protocol conformance
# ---------------------------------------------------------------------------


class TestEmbeddingProviderProtocol:
    """Verify that OllamaEmbedder and OpenAIEmbedder satisfy the Protocol."""

    def test_onnx_has_required_attributes(self):
        embedder = OnnxEmbedder(_onnx_config())
        assert hasattr(embedder, "dimension")
        assert hasattr(embedder, "model_name")
        assert hasattr(embedder, "embed_texts")
        assert hasattr(embedder, "embed_query")
        assert hasattr(embedder, "close")

    def test_ollama_has_required_attributes(self):
        embedder = OllamaEmbedder(_ollama_config())
        assert hasattr(embedder, "dimension")
        assert hasattr(embedder, "model_name")
        assert hasattr(embedder, "embed_texts")
        assert hasattr(embedder, "embed_query")
        assert hasattr(embedder, "close")

    def test_openai_has_required_attributes(self):
        embedder = OpenAIEmbedder(_openai_config())
        assert hasattr(embedder, "dimension")
        assert hasattr(embedder, "model_name")
        assert hasattr(embedder, "embed_texts")
        assert hasattr(embedder, "embed_query")
        assert hasattr(embedder, "close")

    def test_ollama_dimension_and_model(self):
        embedder = OllamaEmbedder(_ollama_config(dimension=768, model="nomic-embed-text"))
        assert embedder.dimension == 768
        assert embedder.model_name == "nomic-embed-text"

    def test_openai_dimension_and_model(self):
        embedder = OpenAIEmbedder(_openai_config(dimension=1536, model="text-embedding-3-small"))
        assert embedder.dimension == 1536
        assert embedder.model_name == "text-embedding-3-small"


# ---------------------------------------------------------------------------
# 2. OllamaEmbedder — mocked httpx
# ---------------------------------------------------------------------------


class TestOllamaEmbedder:
    async def test_embed_batch_returns_vectors(self):
        """embed_texts with two texts returns two vectors."""
        config = _ollama_config(dimension=3)
        embedder = OllamaEmbedder(config)
        fake_resp = _make_httpx_response(
            json_data={"embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]},
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=fake_resp)
        embedder._client = mock_client

        result = await embedder.embed_texts(["hello", "world"])

        assert len(result) == 2
        assert result[0] == [0.1, 0.2, 0.3]
        assert result[1] == [0.4, 0.5, 0.6]
        mock_client.post.assert_called_once_with(
            "/api/embed",
            json={"model": "nomic-embed-text", "input": ["hello", "world"]},
        )

    async def test_embed_query_returns_single_vector(self):
        """embed_query delegates to embed_texts and returns first vector."""
        config = _ollama_config(dimension=2)
        embedder = OllamaEmbedder(config)
        fake_resp = _make_httpx_response(
            json_data={"embeddings": [[1.0, 2.0]]},
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=fake_resp)
        embedder._client = mock_client

        result = await embedder.embed_query("test query")

        assert result == [1.0, 2.0]

    async def test_batch_splitting(self):
        """When batch_size < len(texts), multiple batches are sent."""
        config = _ollama_config(dimension=2, batch_size=2)
        embedder = OllamaEmbedder(config)

        call_count = 0

        async def _fake_post(url, json):
            nonlocal call_count
            call_count += 1
            n = len(json["input"])
            return _make_httpx_response(
                json_data={"embeddings": [[float(call_count)] * 2] * n},
            )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = _fake_post
        embedder._client = mock_client

        result = await embedder.embed_texts(["a", "b", "c", "d", "e"])

        assert len(result) == 5
        # Two full batches of 2, plus one batch of 1 => 3 calls
        assert call_count == 3

    @patch("memtomem.embedding.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_connection_error_raises_embedding_error(self, mock_sleep):
        """ConnectError is wrapped in EmbeddingError with helpful message."""
        config = _ollama_config()
        embedder = OllamaEmbedder(config)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        embedder._client = mock_client

        with pytest.raises(EmbeddingError, match="Cannot connect to Ollama"):
            await embedder.embed_texts(["test"])

    @patch("memtomem.embedding.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_timeout_raises_embedding_error(self, mock_sleep):
        """TimeoutException is wrapped in EmbeddingError."""
        config = _ollama_config()
        embedder = OllamaEmbedder(config)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        embedder._client = mock_client

        with pytest.raises(EmbeddingError, match="timed out"):
            await embedder.embed_texts(["test"])

    async def test_404_raises_model_not_found(self):
        """HTTP 404 produces a helpful 'model not found' message."""
        config = _ollama_config(model="nonexistent-model")
        embedder = OllamaEmbedder(config)
        resp_404 = _make_httpx_response(status_code=404, json_data={})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=resp_404)
        embedder._client = mock_client

        with pytest.raises(EmbeddingError, match="not found"):
            await embedder.embed_texts(["test"])

    async def test_missing_embeddings_key_raises(self):
        """Unexpected API response (no 'embeddings' key) raises EmbeddingError."""
        config = _ollama_config()
        embedder = OllamaEmbedder(config)
        bad_resp = _make_httpx_response(json_data={"something_else": []})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=bad_resp)
        embedder._client = mock_client

        with pytest.raises(EmbeddingError, match="missing 'embeddings' key"):
            await embedder.embed_texts(["test"])

    async def test_close_clears_client(self):
        """close() calls aclose() on the httpx client and sets it to None."""
        config = _ollama_config()
        embedder = OllamaEmbedder(config)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        embedder._client = mock_client

        await embedder.close()

        mock_client.aclose.assert_awaited_once()
        assert embedder._client is None

    async def test_close_without_client_is_noop(self):
        """close() when no client has been created does nothing."""
        config = _ollama_config()
        embedder = OllamaEmbedder(config)
        assert embedder._client is None
        await embedder.close()  # should not raise
        assert embedder._client is None


# ---------------------------------------------------------------------------
# 3. OpenAIEmbedder — mocked httpx
# ---------------------------------------------------------------------------


class TestOpenAIEmbedder:
    async def test_embed_batch_returns_vectors(self):
        """embed_texts returns sorted-by-index vectors."""
        config = _openai_config(dimension=3)
        embedder = OpenAIEmbedder(config)
        fake_resp = _make_httpx_response(
            json_data={
                "data": [
                    {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                    {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                ],
            },
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=fake_resp)
        embedder._client = mock_client

        result = await embedder.embed_texts(["hello", "world"])

        assert len(result) == 2
        # Data is sorted by index, so index=0 comes first
        assert result[0] == [0.1, 0.2, 0.3]
        assert result[1] == [0.4, 0.5, 0.6]

    async def test_embed_texts_empty_input_returns_empty(self):
        """Empty input list returns empty result without calling the API."""
        config = _openai_config()
        embedder = OpenAIEmbedder(config)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        embedder._client = mock_client

        result = await embedder.embed_texts([])

        assert result == []
        mock_client.post.assert_not_called()

    async def test_api_key_in_headers(self):
        """API key is sent as Bearer token in Authorization header."""
        config = _openai_config(api_key="sk-my-secret")
        embedder = OpenAIEmbedder(config)
        client = embedder._get_client()
        assert client.headers["Authorization"] == "Bearer sk-my-secret"
        await client.aclose()

    async def test_no_api_key_omits_auth_header(self):
        """When api_key is empty, no Authorization header is set."""
        config = _openai_config(api_key="")
        embedder = OpenAIEmbedder(config)
        client = embedder._get_client()
        assert "Authorization" not in client.headers
        await client.aclose()

    @patch("memtomem.embedding.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_rate_limit_429_raises_embedding_error(self, mock_sleep):
        """HTTP 429 triggers retries and ultimately raises EmbeddingError."""
        config = _openai_config()
        embedder = OpenAIEmbedder(config)

        resp_429 = _make_httpx_response(
            status_code=429,
            json_data={},
            headers={"retry-after": "0"},
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=resp_429)
        embedder._client = mock_client

        with pytest.raises(EmbeddingError, match="rate limit"):
            await embedder.embed_texts(["test"])

    async def test_auth_error_401_raises_embedding_error(self):
        """HTTP 401 produces authentication failure message."""
        config = _openai_config()
        embedder = OpenAIEmbedder(config)
        resp_401 = _make_httpx_response(status_code=401, json_data={})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=resp_401)
        embedder._client = mock_client

        with pytest.raises(EmbeddingError, match="authentication failed"):
            await embedder.embed_texts(["test"])

    @patch("memtomem.embedding.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_connection_error_raises_embedding_error(self, mock_sleep):
        """ConnectError is wrapped in EmbeddingError."""
        config = _openai_config()
        embedder = OpenAIEmbedder(config)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        embedder._client = mock_client

        with pytest.raises(EmbeddingError, match="Cannot connect to OpenAI"):
            await embedder.embed_texts(["test"])

    async def test_close_clears_client(self):
        config = _openai_config()
        embedder = OpenAIEmbedder(config)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        embedder._client = mock_client

        await embedder.close()

        mock_client.aclose.assert_awaited_once()
        assert embedder._client is None


# ---------------------------------------------------------------------------
# 3.5. OnnxEmbedder — mocked fastembed
# ---------------------------------------------------------------------------


def _make_fake_embedding_model(vectors: list[list[float]]):
    """Return a mock fastembed TextEmbedding whose embed() yields vectors."""
    import numpy as np

    model = MagicMock()
    model.embed.return_value = iter(np.array(v) for v in vectors)
    return model


class TestOnnxEmbedder:
    def test_dimension_and_model(self):
        embedder = OnnxEmbedder(_onnx_config(dimension=384, model="all-MiniLM-L6-v2"))
        assert embedder.dimension == 384
        assert embedder.model_name == "all-MiniLM-L6-v2"

    @pytest.mark.anyio
    async def test_embed_texts_returns_vectors(self):
        """embed_texts returns list of float lists via mocked fastembed."""
        config = _onnx_config(dimension=3)
        embedder = OnnxEmbedder(config)
        embedder._model = _make_fake_embedding_model([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])

        result = await embedder.embed_texts(["hello", "world"])

        assert len(result) == 2
        assert result[0] == pytest.approx([0.1, 0.2, 0.3])
        assert result[1] == pytest.approx([0.4, 0.5, 0.6])

    @pytest.mark.anyio
    async def test_embed_query_returns_single_vector(self):
        config = _onnx_config(dimension=2)
        embedder = OnnxEmbedder(config)
        embedder._model = _make_fake_embedding_model([[1.0, 2.0]])

        result = await embedder.embed_query("test query")

        assert result == pytest.approx([1.0, 2.0])

    @pytest.mark.anyio
    async def test_embed_texts_empty_input(self):
        embedder = OnnxEmbedder(_onnx_config())
        result = await embedder.embed_texts([])
        assert result == []

    @pytest.mark.anyio
    async def test_embed_query_empty_raises(self):
        embedder = OnnxEmbedder(_onnx_config())
        with pytest.raises(EmbeddingError, match="empty"):
            await embedder.embed_query("")

    @pytest.mark.anyio
    async def test_fastembed_not_installed_raises(self):
        """ImportError from fastembed is wrapped with install instructions."""
        config = _onnx_config()
        embedder = OnnxEmbedder(config)
        embedder._model = None  # ensure lazy init triggers

        with patch.dict("sys.modules", {"fastembed": None}):
            with pytest.raises(EmbeddingError, match="pip install memtomem\\[onnx\\]"):
                await embedder.embed_texts(["test"])

    @pytest.mark.anyio
    async def test_inference_error_wrapped(self):
        """Model inference exception is wrapped in EmbeddingError."""
        config = _onnx_config()
        embedder = OnnxEmbedder(config)
        bad_model = MagicMock()
        bad_model.embed.side_effect = RuntimeError("ONNX inference failed")
        embedder._model = bad_model

        with pytest.raises(EmbeddingError, match="ONNX embedding failed"):
            await embedder.embed_texts(["test"])

    @pytest.mark.anyio
    async def test_close_clears_model(self):
        config = _onnx_config()
        embedder = OnnxEmbedder(config)
        embedder._model = MagicMock()

        await embedder.close()

        assert embedder._model is None

    @pytest.mark.anyio
    async def test_close_without_model_is_noop(self):
        embedder = OnnxEmbedder(_onnx_config())
        assert embedder._model is None
        await embedder.close()
        assert embedder._model is None

    def test_threads_default_is_zero(self):
        """Default preserves prior behavior — ORT picks all physical cores."""
        assert _onnx_config().threads == 0

    def test_threads_rejects_negative(self):
        """Validator catches typos like -1 at config-load time."""
        with pytest.raises(ValueError, match="non-negative"):
            EmbeddingConfig(provider="onnx", model="bge-m3", dimension=1024, threads=-1)

    @pytest.mark.anyio
    async def test_threads_forwarded_to_fastembed(self):
        """threads=N reaches fastembed.TextEmbedding(threads=N)."""
        config = _onnx_config(threads=4)
        embedder = OnnxEmbedder(config)
        fake_model = _make_fake_embedding_model([[0.1, 0.2, 0.3]])
        with (
            patch("memtomem.embedding.onnx._register_custom_models_if_needed"),
            patch("fastembed.TextEmbedding", return_value=fake_model) as mock_te,
        ):
            await embedder.embed_texts(["hi"])
        mock_te.assert_called_once()
        assert mock_te.call_args.kwargs["threads"] == 4

    @pytest.mark.anyio
    async def test_threads_zero_passes_none_to_fastembed(self):
        """threads=0 → None so fastembed/ORT keeps its default behavior."""
        config = _onnx_config(threads=0)
        embedder = OnnxEmbedder(config)
        fake_model = _make_fake_embedding_model([[0.1, 0.2, 0.3]])
        with (
            patch("memtomem.embedding.onnx._register_custom_models_if_needed"),
            patch("fastembed.TextEmbedding", return_value=fake_model) as mock_te,
        ):
            await embedder.embed_texts(["hi"])
        assert mock_te.call_args.kwargs["threads"] is None


# ---------------------------------------------------------------------------
# 4. create_embedder factory
# ---------------------------------------------------------------------------


class TestCreateEmbedder:
    def test_onnx_provider(self):
        config = _onnx_config(provider="onnx")
        embedder = create_embedder(config)
        assert isinstance(embedder, OnnxEmbedder)

    def test_ollama_provider(self):
        config = _ollama_config(provider="ollama")
        embedder = create_embedder(config)
        assert isinstance(embedder, OllamaEmbedder)

    def test_openai_provider(self):
        config = _openai_config(provider="openai")
        embedder = create_embedder(config)
        assert isinstance(embedder, OpenAIEmbedder)

    def test_provider_case_insensitive(self):
        config = _ollama_config(provider="OLLAMA")
        embedder = create_embedder(config)
        assert isinstance(embedder, OllamaEmbedder)

    def test_none_provider(self):
        config = EmbeddingConfig(provider="none", model="", dimension=0, base_url="")
        embedder = create_embedder(config)
        assert isinstance(embedder, NoopEmbedder)

    def test_unknown_provider_raises_config_error(self):
        config = _ollama_config(provider="unknown_backend")
        with pytest.raises(ConfigError, match="Unknown embedding provider"):
            create_embedder(config)


# ---------------------------------------------------------------------------
# 4.5. NoopEmbedder (BM25-only mode)
# ---------------------------------------------------------------------------


class TestNoopEmbedder:
    """Verify NoopEmbedder satisfies the EmbeddingProvider protocol."""

    def test_dimension_is_zero(self):
        embedder = NoopEmbedder()
        assert embedder.dimension == 0

    def test_model_name_is_none(self):
        embedder = NoopEmbedder()
        assert embedder.model_name == "none"

    @pytest.mark.anyio
    async def test_embed_texts_returns_empty_lists(self):
        embedder = NoopEmbedder()
        result = await embedder.embed_texts(["hello", "world"])
        assert result == [[], []]

    @pytest.mark.anyio
    async def test_embed_texts_empty_input(self):
        embedder = NoopEmbedder()
        result = await embedder.embed_texts([])
        assert result == []

    @pytest.mark.anyio
    async def test_embed_query_returns_empty_list(self):
        embedder = NoopEmbedder()
        result = await embedder.embed_query("test query")
        assert result == []

    @pytest.mark.anyio
    async def test_close_is_noop(self):
        embedder = NoopEmbedder()
        await embedder.close()  # should not raise


# ---------------------------------------------------------------------------
# 5. Retry logic — with_retry decorator and _parse_retry_after
# ---------------------------------------------------------------------------


class TestRetryDecorator:
    async def test_succeeds_first_try(self):
        """Function succeeds on first attempt — no retries."""
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.0, retryable_exceptions=(ValueError,))
        async def ok():
            nonlocal call_count
            call_count += 1
            return "ok"

        assert await ok() == "ok"
        assert call_count == 1

    async def test_retries_on_transient_error(self):
        """Retries on retryable exception, succeeds on later attempt."""
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.0, retryable_exceptions=(ValueError,))
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("transient")
            return "recovered"

        assert await flaky() == "recovered"
        assert call_count == 3

    async def test_max_retries_exceeded_raises(self):
        """After exhausting all attempts, the last exception is raised."""
        call_count = 0

        @with_retry(max_attempts=2, base_delay=0.0, retryable_exceptions=(RuntimeError,))
        async def always_fail():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("permanent-ish")

        with pytest.raises(RuntimeError, match="permanent-ish"):
            await always_fail()
        assert call_count == 2

    async def test_non_retryable_error_propagates_immediately(self):
        """Non-retryable exception is raised on first attempt without retries."""
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.0, retryable_exceptions=(ValueError,))
        async def type_err():
            nonlocal call_count
            call_count += 1
            raise TypeError("not retryable")

        with pytest.raises(TypeError, match="not retryable"):
            await type_err()
        assert call_count == 1

    def test_invalid_max_attempts(self):
        """max_attempts < 1 raises ValueError at decoration time."""
        with pytest.raises(ValueError, match="max_attempts"):

            @with_retry(max_attempts=0)
            async def noop():
                pass  # pragma: no cover

    def test_invalid_base_delay(self):
        """Negative base_delay raises ValueError at decoration time."""
        with pytest.raises(ValueError, match="base_delay"):

            @with_retry(base_delay=-1.0)
            async def noop():
                pass  # pragma: no cover


class TestParseRetryAfter:
    def test_none_input(self):
        assert _parse_retry_after(None) is None

    def test_empty_string(self):
        assert _parse_retry_after("") is None

    def test_numeric_seconds(self):
        assert _parse_retry_after("5") == 5.0

    def test_float_seconds(self):
        assert _parse_retry_after("1.5") == 1.5

    def test_unparseable_string(self):
        assert _parse_retry_after("not-a-date-or-number") is None

    def test_rfc7231_date(self):
        """A valid HTTP-date in the future returns a positive delay."""
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        future = datetime.now(timezone.utc) + timedelta(seconds=30)
        header = format_datetime(future, usegmt=True)
        result = _parse_retry_after(header)
        assert result is not None
        # Should be roughly 30s (allow some tolerance for test execution time)
        assert 25.0 <= result <= 35.0

    def test_rfc7231_date_in_past_returns_zero(self):
        """An HTTP-date in the past returns 0 (no negative delay)."""
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        header = format_datetime(past, usegmt=True)
        result = _parse_retry_after(header)
        assert result is not None
        assert result == 0.0
