"""Comprehensive tests for memtomem embedding providers.

Covers:
  - EmbeddingProvider protocol conformance
  - OllamaEmbedder (mocked httpx)
  - OpenAIEmbedder (mocked httpx)
  - OnnxEmbedder (mocked fastembed)
  - create_embedder factory
  - with_retry decorator & parse_retry_after helper
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from memtomem.config import EmbeddingConfig
from memtomem.embedding.factory import create_embedder
from memtomem.embedding.noop import NoopEmbedder
from memtomem.embedding.ollama import OllamaEmbedder
from memtomem.embedding.onnx import OnnxEmbedder
from memtomem.embedding.openai import OpenAIEmbedder
from memtomem.embedding.retry import parse_retry_after, with_retry
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

    def test_preferred_concurrency_hints(self):
        """#1783: local CPU inference advertises 1 (concurrent ``session.run``
        multiplies peak activation memory); remote latency-bound providers
        match the engine's file-level cap. Noop is inert but keeps the
        contract uniform across built-ins."""
        assert OnnxEmbedder(_onnx_config()).preferred_concurrency == 1
        assert OllamaEmbedder(_ollama_config()).preferred_concurrency == 8
        assert OpenAIEmbedder(_openai_config()).preferred_concurrency == 8
        assert NoopEmbedder().preferred_concurrency == 8

    def test_preferred_concurrency_read_is_side_effect_free(self):
        """The engine reads the hint before any embedding happens — it must
        never trigger the lazy (and potentially downloading) model load."""
        embedder = OnnxEmbedder(_onnx_config())
        _ = embedder.preferred_concurrency
        assert embedder._model is None
        assert embedder._loading is False


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

    @patch("memtomem.embedding.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_transient_503_is_retried_then_succeeds(self, mock_sleep):
        """A 503 (model still loading after ``ollama serve`` restart) uses the
        retry backoff instead of aborting on the first attempt (#1574 item 2).
        """
        config = _ollama_config(dimension=2)
        embedder = OllamaEmbedder(config)
        ok_resp = _make_httpx_response(json_data={"embeddings": [[1.0, 2.0]]})
        resp_503 = _make_httpx_response(status_code=503, json_data={})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=[resp_503, resp_503, ok_resp])
        embedder._client = mock_client

        result = await embedder.embed_texts(["test"])

        assert result == [[1.0, 2.0]]
        assert mock_client.post.await_count == 3

    @patch("memtomem.embedding.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_persistent_503_exhausts_retries_as_embedding_error(self, mock_sleep):
        """503 on every attempt exhausts the backoff and surfaces as a
        terminal EmbeddingError (never a raw RateLimitError/HTTPStatusError)."""
        config = _ollama_config()
        embedder = OllamaEmbedder(config)
        resp_503 = _make_httpx_response(status_code=503, json_data={})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=resp_503)
        embedder._client = mock_client

        with pytest.raises(EmbeddingError, match="transient HTTP error after retries") as ei:
            await embedder.embed_texts(["test"])
        assert mock_client.post.await_count == 3  # max_attempts, not 1
        # The status-aware sentinel message surfaces — not "Rate limited",
        # which would mislabel a server reload as rate limiting.
        assert "HTTP 503" in str(ei.value)
        assert "Rate limited" not in str(ei.value)

    @patch("memtomem.embedding.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_503_honors_retry_after_header(self, mock_sleep):
        """A Retry-After header on the transient response drives the sleep,
        matching the OpenAI provider's 429 handling."""
        config = _ollama_config(dimension=2)
        embedder = OllamaEmbedder(config)
        resp_503 = _make_httpx_response(status_code=503, json_data={}, headers={"retry-after": "7"})
        ok_resp = _make_httpx_response(json_data={"embeddings": [[1.0, 2.0]]})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=[resp_503, ok_resp])
        embedder._client = mock_client

        result = await embedder.embed_texts(["test"])

        assert result == [[1.0, 2.0]]
        mock_sleep.assert_awaited_once_with(7.0)

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

    async def test_short_embedding_array_raises(self):
        """A batch returning fewer vectors than inputs raises (issue #1563).

        Silent ``zip`` truncation would land BM25-only chunks whose
        content_hash is still committed, poisoning the re-index skip forever.
        """
        config = _ollama_config(dimension=2)
        embedder = OllamaEmbedder(config)
        # Three inputs, only two vectors back.
        short_resp = _make_httpx_response(
            json_data={"embeddings": [[1.0, 2.0], [3.0, 4.0]]},
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=short_resp)
        embedder._client = mock_client

        with pytest.raises(EmbeddingError, match="refusing to truncate"):
            await embedder.embed_texts(["a", "b", "c"])

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

    async def test_short_data_array_raises(self):
        """A response dropping a ``data`` item raises rather than truncating.

        Same failure class as OllamaEmbedder (issue #1563): a short array must
        not silently drop trailing chunks' vectors downstream.
        """
        config = _openai_config(dimension=3)
        embedder = OpenAIEmbedder(config)
        # Two inputs, only one vector back.
        short_resp = _make_httpx_response(
            json_data={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=short_resp)
        embedder._client = mock_client

        with pytest.raises(EmbeddingError, match="refusing to truncate"):
            await embedder.embed_texts(["hello", "world"])

    async def test_malformed_indices_raise(self):
        """Right count, wrong indices (duplicate/non-contiguous) must raise.

        Issue #1563 alignment variant: two records for index=0 and none for
        index=1 pass the length guard, but positional extraction would map the
        wrong vector to a chunk — a silent mis-embedding the content-hash skip
        preserves permanently.
        """
        config = _openai_config(dimension=3)
        embedder = OpenAIEmbedder(config)
        # Two records, correct count, but index=1 is missing (index=0 dupes).
        malformed_resp = _make_httpx_response(
            json_data={
                "data": [
                    {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                    {"index": 0, "embedding": [0.4, 0.5, 0.6]},
                ],
            },
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=malformed_resp)
        embedder._client = mock_client

        with pytest.raises(EmbeddingError, match="malformed embedding indices"):
            await embedder.embed_texts(["hello", "world"])

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

    @pytest.mark.anyio
    async def test_close_waits_for_loading_worker_no_resurrection(self):
        """#1792 review: close() must drain the inference worker before
        clearing ``_model``. If a worker is still inside ``_get_model()``
        constructing the model when close() runs, a fire-and-forget
        shutdown would let it re-assign the freshly-loaded model right after
        close() nulled it — resurrecting the ORT session past close() and
        leaking the mmap (#206). Pins: (a) close() blocks while the
        constructor is in flight, and (b) ``_model`` is None afterwards."""
        import numpy as np

        embedder = OnnxEmbedder(_onnx_config(dimension=2))

        constructing = threading.Event()  # worker entered TextEmbedding(...)
        release = threading.Event()  # unblocks construction

        class _BlockingTextEmbedding:
            def __init__(self, *a, **k):
                constructing.set()
                assert release.wait(timeout=10), "release never set"

            def embed(self, texts):
                return iter(np.array([0.0, 0.0]) for _ in texts)

        with (
            patch("memtomem.embedding.onnx._register_custom_models_if_needed"),
            patch("fastembed.TextEmbedding", _BlockingTextEmbedding),
        ):
            embed_task = asyncio.create_task(embedder.embed_texts(["x"]))
            # Worker is now blocked inside the model constructor.
            assert await asyncio.to_thread(constructing.wait, 10)

            close_task = asyncio.create_task(embedder.close())
            # close() must NOT complete while the constructor is in flight —
            # proves it drains rather than clearing _model out from under the
            # worker (which would resurrect the model on assignment).
            await asyncio.sleep(0.1)
            assert not close_task.done(), "close() returned before worker drained"

            release.set()
            await asyncio.wait_for(close_task, timeout=10)
            await asyncio.wait_for(embed_task, timeout=10)

        # The worker assigned _model during construction, but close()'s
        # drain-then-clear ran strictly after that assignment.
        assert embedder._model is None

    @pytest.mark.anyio
    async def test_get_model_after_close_refuses(self):
        """The ``_closed`` latch covers *every* load path, not just inference
        on ``_infer_executor``: any ``_get_model`` after close (e.g. a warmup
        load on the default executor) must refuse rather than resurrect the
        ORT session (#1792 review — warmup path)."""
        embedder = OnnxEmbedder(_onnx_config())
        await embedder.close()
        # _get_model is the shared chokepoint for inference AND warmup loads.
        with pytest.raises(EmbeddingError, match="closed"):
            await asyncio.to_thread(embedder._get_model)
        assert embedder._model is None

    @pytest.mark.anyio
    async def test_close_cancelled_still_clears_model(self):
        """Cancelling the coroutine awaiting close() must not leave the model
        alive: teardown runs in ``_close_sync`` on a worker thread, which
        completes regardless (#1792 review — cancelled-close path)."""
        import numpy as np

        embedder = OnnxEmbedder(_onnx_config(dimension=2))

        constructing = threading.Event()
        release = threading.Event()

        class _BlockingTextEmbedding:
            def __init__(self, *a, **k):
                constructing.set()
                assert release.wait(timeout=10), "release never set"

            def embed(self, texts):
                return iter(np.array([0.0, 0.0]) for _ in texts)

        with (
            patch("memtomem.embedding.onnx._register_custom_models_if_needed"),
            patch("fastembed.TextEmbedding", _BlockingTextEmbedding),
        ):
            embed_task = asyncio.create_task(embedder.embed_texts(["x"]))
            assert await asyncio.to_thread(constructing.wait, 10)

            close_task = asyncio.create_task(embedder.close())
            await asyncio.sleep(0.05)  # let close() reach its run_in_executor await
            close_task.cancel()
            # Unblock the loader so _close_sync (waiting on _load_lock) can
            # finish — close() settles its teardown future before propagating
            # the cancellation, so awaiting the cancelled task IS the proof
            # that teardown completed.
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await close_task
            with contextlib.suppress(Exception):
                await asyncio.wait_for(embed_task, timeout=10)

        assert embedder._closed is True
        assert embedder._model is None

    @pytest.mark.anyio
    async def test_close_cancelled_while_queued_still_tears_down(self):
        """#1792 round 5: cancelling close() while ``_close_sync`` is still
        QUEUED in the default executor (no free worker yet) must not cancel
        the teardown — a bare ``await run_in_executor`` would cancel the
        not-yet-started future, leaving ``_model`` alive and the inference
        executor running forever. The shield keeps the future alive; it runs
        once a worker frees."""
        from concurrent.futures import ThreadPoolExecutor as _TPE

        embedder = OnnxEmbedder(_onnx_config())
        embedder._model = MagicMock()

        loop = asyncio.get_running_loop()
        blocker_release = threading.Event()
        # Single-worker default executor so _close_sync deterministically
        # queues behind the blocker instead of starting.
        small = _TPE(max_workers=1, thread_name_prefix="test-default")
        loop.set_default_executor(small)
        try:
            blocker = loop.run_in_executor(None, blocker_release.wait, 10)
            await asyncio.sleep(0.05)  # blocker occupies the lone worker

            close_task = asyncio.create_task(embedder.close())
            await asyncio.sleep(0.05)  # _close_sync submitted -> queued
            close_task.cancel()

            # Teardown cannot have run yet (worker still blocked) — and must
            # not have been lost with the cancellation.
            assert embedder._closed is False

            blocker_release.set()
            with pytest.raises(asyncio.CancelledError):
                await close_task  # settles the shielded teardown, then raises
            await blocker

            assert embedder._closed is True
            assert embedder._model is None
        finally:
            blocker_release.set()
            small.shutdown(wait=False)

    @pytest.mark.anyio
    async def test_close_double_cancel_still_tears_down(self):
        """#1792 round 6: a SECOND cancellation delivered while close() is
        already settling its cancelled teardown must not pierce through to
        the queued ``_close_sync`` — an unshielded settle-await lets the
        second cancel cancel the executor future and lose the teardown.
        Every settle iteration is shielded, so teardown survives repeated
        cancellation."""
        from concurrent.futures import ThreadPoolExecutor as _TPE

        embedder = OnnxEmbedder(_onnx_config())
        embedder._model = MagicMock()

        loop = asyncio.get_running_loop()
        blocker_release = threading.Event()
        small = _TPE(max_workers=1, thread_name_prefix="test-default")
        loop.set_default_executor(small)
        try:
            blocker = loop.run_in_executor(None, blocker_release.wait, 10)
            await asyncio.sleep(0.05)  # blocker occupies the lone worker

            close_task = asyncio.create_task(embedder.close())
            await asyncio.sleep(0.05)  # _close_sync submitted -> queued
            close_task.cancel()
            await asyncio.sleep(0.05)  # close() is now settling (shielded)
            close_task.cancel()  # second cancel, mid-settle
            await asyncio.sleep(0.05)

            # Teardown still pending (worker blocked) — and still alive.
            assert embedder._closed is False

            blocker_release.set()
            with pytest.raises(asyncio.CancelledError):
                await close_task
            await blocker

            assert embedder._closed is True
            assert embedder._model is None
        finally:
            blocker_release.set()
            small.shutdown(wait=False)

    @staticmethod
    def _blocking_model(stats, stats_lock, entered, release):
        """A fake fastembed model whose ``embed`` records each entry and
        blocks on ``release``, so a test can hold one inference open and
        observe whether a second one starts."""

        class _BlockingModel:
            def embed(self, texts):
                import numpy as np

                with stats_lock:
                    stats["entries"].append(texts[0] if texts else None)
                    stats["inflight"] += 1
                    stats["peak"] = max(stats["peak"], stats["inflight"])
                entered.set()
                assert release.wait(timeout=10), "release event never set"
                with stats_lock:
                    stats["inflight"] -= 1
                return iter(np.array([0.0, 0.0]) for _ in texts)

        return _BlockingModel()

    @pytest.mark.anyio
    async def test_inference_serialized_and_cancel_preserves_cap(self):
        """#1783: the dedicated single-worker ``_infer_executor`` caps
        concurrent ONNX inference at 1 and survives task cancellation.

        Cancelling the coroutine awaiting a *running* inference cannot stop
        it (ORT has no mid-run interrupt) and frees the async slot, but the
        lone executor worker stays busy — so a follow-up ``embed_texts``
        queues instead of starting a second concurrent ``session.run``.
        """
        embedder = OnnxEmbedder(_onnx_config(dimension=2))
        stats_lock = threading.Lock()
        stats = {"entries": [], "inflight": 0, "peak": 0}
        entered = threading.Event()
        release = threading.Event()
        embedder._model = self._blocking_model(stats, stats_lock, entered, release)

        task2 = None
        try:
            task1 = asyncio.create_task(embedder.embed_texts(["first"]))
            assert await asyncio.to_thread(entered.wait, 10)

            # Cancelling task1 abandons the running worker; the executor's
            # one thread is still occupied by "first".
            task1.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task1

            entered.clear()
            task2 = asyncio.create_task(embedder.embed_texts(["second"]))
            # "second" is queued behind the still-running "first"; it must
            # not enter the model. A short settle window is a positive
            # check that queuing holds (peak can only ever be 1 here).
            await asyncio.sleep(0.1)
            with stats_lock:
                assert stats["entries"] == ["first"]
                assert stats["peak"] == 1
        finally:
            release.set()

        result = await asyncio.wait_for(task2, timeout=10)
        assert result == [pytest.approx([0.0, 0.0])]
        with stats_lock:
            assert stats["entries"] == ["first", "second"]
            assert stats["peak"] == 1

    @pytest.mark.anyio
    async def test_cancelled_queued_inference_never_runs(self):
        """A call cancelled while *queued* (its future not yet started in the
        executor) must never run — the dedicated executor cancels the queued
        future. This is the property a threading-semaphore gate could not
        give: there, an abandoned worker blocked on the gate would still run
        its inference once the gate freed (#1792 review)."""
        embedder = OnnxEmbedder(_onnx_config(dimension=2))
        stats_lock = threading.Lock()
        stats = {"entries": [], "inflight": 0, "peak": 0}
        entered = threading.Event()
        release = threading.Event()
        embedder._model = self._blocking_model(stats, stats_lock, entered, release)

        try:
            task_a = asyncio.create_task(embedder.embed_texts(["active"]))
            assert await asyncio.to_thread(entered.wait, 10)  # "active" holds the worker

            # "queued" cannot start (worker busy); cancel it while queued.
            task_q = asyncio.create_task(embedder.embed_texts(["queued"]))
            await asyncio.sleep(0.05)
            task_q.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task_q
        finally:
            release.set()

        result_a = await asyncio.wait_for(task_a, timeout=10)
        assert result_a == [pytest.approx([0.0, 0.0])]
        # Let any (wrongly) un-cancelled queued future have its chance to run.
        await asyncio.sleep(0.1)
        with stats_lock:
            assert "queued" not in stats["entries"]
            assert stats["entries"] == ["active"]

    def test_threads_default_is_four(self):
        """Default caps ONNX at 4 cores so a bulk reindex doesn't pin every
        physical core. #640 follow-up: pre-flip the default was 0 (= ORT
        default = all cores) which made indexing feel like a hang because
        nothing else on the machine could respond. Users on dedicated
        servers can opt back into all-cores by explicitly setting threads=0.
        """
        assert _onnx_config().threads == 4

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
# 5. Retry logic — with_retry decorator and parse_retry_after
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
        assert parse_retry_after(None) is None

    def test_empty_string(self):
        assert parse_retry_after("") is None

    def test_numeric_seconds(self):
        assert parse_retry_after("5") == 5.0

    def test_float_seconds(self):
        assert parse_retry_after("1.5") == 1.5

    def test_unparseable_string(self):
        assert parse_retry_after("not-a-date-or-number") is None

    def test_rfc7231_date(self):
        """A valid HTTP-date in the future returns a positive delay."""
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        future = datetime.now(timezone.utc) + timedelta(seconds=30)
        header = format_datetime(future, usegmt=True)
        result = parse_retry_after(header)
        assert result is not None
        # Should be roughly 30s (allow some tolerance for test execution time)
        assert 25.0 <= result <= 35.0

    def test_rfc7231_date_in_past_returns_zero(self):
        """An HTTP-date in the past returns 0 (no negative delay)."""
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        header = format_datetime(past, usegmt=True)
        result = parse_retry_after(header)
        assert result is not None
        assert result == 0.0
