"""A failed retrieval leg is reported without its exception text (#2675).

``bm25_error`` / ``dense_error`` reach the MCP response, the Web API and the
result cache. An embedder's message can carry its endpoint, user-info and
query-string credentials included, so the pipeline records a memtomem error's
vetted ``public_message`` when it has one and the error's class otherwise.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from memtomem.config import EmbeddingConfig
from memtomem.embedding._dependencies import missing_onnx_dependency
from memtomem.embedding.ollama import OllamaEmbedder
from memtomem.embedding.onnx import OnnxEmbedder
from memtomem.embedding.openai import OpenAIEmbedder
from memtomem.errors import (
    EmbeddingError,
    ExhaustiveDenseSearchLimitError,
    QueryEmbeddingDimensionError,
    RetryableEmbeddingError,
    SchemaDowngradeError,
)
from memtomem.search.pipeline import _leg_error_text
from memtomem.server.context import AppContext
from memtomem.server.tools.search import mem_search
from memtomem.web.schemas.core import RetrievalStatsOut

from helpers import StubCtx

_ENDPOINT = "http://user:hunter2@ollama.internal:11434/?token=opaque-tok"
_LEAKS = ("hunter2", "ollama.internal", "opaque-tok")


def _clean(text: str) -> bool:
    return not any(leak in text for leak in _LEAKS)


class TestLegErrorText:
    def test_vetted_message_is_shown(self):
        exc = EmbeddingError.vetted("Run 'ollama pull nomic' to download it.")
        assert _leg_error_text(exc) == "Run 'ollama pull nomic' to download it."

    def test_unvetted_memtomem_error_shows_its_class_only(self):
        exc = EmbeddingError(f"Ollama embedding failed: {_ENDPOINT}")
        assert _leg_error_text(exc) == "EmbeddingError — details are in the server log"

    def test_retryable_error_says_retry_without_its_text(self):
        exc = RetryableEmbeddingError(f"transport failed at {_ENDPOINT}")
        text = _leg_error_text(exc)
        assert text.startswith("a transient failure (RetryableEmbeddingError)")
        assert _clean(text)

    def test_foreign_exception_shows_its_class_only(self):
        exc = httpx.ConnectError(f"cannot reach {_ENDPOINT}")
        assert _leg_error_text(exc) == "ConnectError — details are in the server log"

    def test_empty_message_still_names_a_failure(self):
        assert _leg_error_text(RuntimeError()) == "RuntimeError — details are in the server log"

    def test_subclass_of_a_vetted_class_is_not_vetted_by_class(self):
        """Passthrough is decided at the raise site, never by exception type."""

        class _Custom(ExhaustiveDenseSearchLimitError):
            pass

        exc = _Custom(f"scan failed: {_ENDPOINT}")
        assert _leg_error_text(exc) == "_Custom — details are in the server log"


@pytest.mark.parametrize(
    "exc_type",
    [QueryEmbeddingDimensionError, ExhaustiveDenseSearchLimitError, SchemaDowngradeError],
)
def test_storage_remediation_errors_carry_public_text_at_every_raise(exc_type):
    """Every raise of these classes in storage keeps its advice for callers."""
    import ast
    import inspect

    from memtomem.storage import sqlite_backend, sqlite_schema, sqlite_search_ops

    raises = []
    for module in (sqlite_backend, sqlite_schema, sqlite_search_ops):
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                call = node.exc
                func = call.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    if func.value.id == exc_type.__name__:
                        raises.append(func.attr)
                elif isinstance(func, ast.Name) and func.id == exc_type.__name__:
                    kws = {kw.arg for kw in call.keywords}
                    raises.append("public_message" if "public_message" in kws else "bare")
    assert raises, f"no raise of {exc_type.__name__} found"
    assert set(raises) <= {"vetted", "public_message"}, raises


class TestSchemaRefusalStaysVettedWhenReraised:
    """The sticky refusal re-raises with text built from the tripping error."""

    def _trip(self, monkeypatch, tripping: SchemaDowngradeError) -> list[SchemaDowngradeError]:
        import sqlite3

        from memtomem.storage import sqlite_backend
        from memtomem.storage.sqlite_backend import SqliteBackend

        def _raise(_conn):
            raise tripping

        monkeypatch.setattr(sqlite_backend, "check_schema_downgrade", _raise)
        backend = SqliteBackend.__new__(SqliteBackend)
        backend._schema_checked_at = {}
        backend._schema_superseded = None
        backend._schema_superseded_public = None
        conn = sqlite3.connect(":memory:")
        seen = []
        for _ in range(2):  # the tripping check, then the sticky refusal
            with pytest.raises(SchemaDowngradeError) as info:
                backend._check_schema_current(conn, force=True)
            seen.append(info.value)
        conn.close()
        return seen

    def test_vetted_refusal_keeps_its_advice(self, monkeypatch):
        tripping = SchemaDowngradeError.vetted("Upgrade memtomem to open it.")
        for exc in self._trip(monkeypatch, tripping):
            assert _leg_error_text(exc).startswith("Upgrade memtomem to open it.")
            assert "restart it on the newer release" in _leg_error_text(exc)

    def test_unvetted_refusal_stays_unvetted(self, monkeypatch):
        tripping = SchemaDowngradeError(f"stamp read from {_ENDPOINT}")
        for exc in self._trip(monkeypatch, tripping):
            assert _leg_error_text(exc) == "SchemaDowngradeError — details are in the server log"


def _ollama(base_url: str = _ENDPOINT, model: str = "nomic-embed-text") -> OllamaEmbedder:
    return OllamaEmbedder(
        EmbeddingConfig(
            provider="ollama",
            model=model,
            dimension=768,
            base_url=base_url,
            api_key="",
            batch_size=64,
            max_concurrent_batches=1,
        )
    )


def _openai() -> OpenAIEmbedder:
    return OpenAIEmbedder(
        EmbeddingConfig(
            provider="openai",
            model="text-embedding-3-small",
            dimension=1536,
            base_url=_ENDPOINT,
            api_key="sk-test-key",
            batch_size=64,
            max_concurrent_batches=1,
        )
    )


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, json={}, request=httpx.Request("POST", _ENDPOINT))


# (outcome of client.post, what callers should see). ``None`` means the site is
# deliberately unvetted, so only the class shows.
_OLLAMA_CASES = {
    "connect": (httpx.ConnectError(f"refused: {_ENDPOINT}"), "verify 'ollama serve' is running"),
    "timeout": (httpx.ReadTimeout(f"slow: {_ENDPOINT}"), "may still be loading"),
    "transient": (_response(503), "transient HTTP error after retries"),
    "missing-model": (_response(404), "ollama pull nomic-embed-text"),
    "other-status": (_response(400), None),
    "transport": (httpx.ReadError(f"reset: {_ENDPOINT}"), None),
}
_OPENAI_CASES = {
    "connect": (httpx.ConnectError(f"refused: {_ENDPOINT}"), "embedding.base_url"),
    "timeout": (httpx.ReadTimeout(f"slow: {_ENDPOINT}"), "may be overloaded"),
    "rate-limit": (_response(429), "rate limit exceeded"),
    "transient": (_response(503), "transient HTTP error after retries"),
    "auth": (_response(401), "Verify your API key"),
    "other-status": (_response(400), None),
    "transport": (httpx.ReadError(f"reset: {_ENDPOINT}"), None),
}


def _mock_client(outcome: object) -> AsyncMock:
    client = AsyncMock(spec=httpx.AsyncClient)
    if isinstance(outcome, BaseException):
        client.post = AsyncMock(side_effect=outcome)
    else:
        client.post = AsyncMock(return_value=outcome)
    return client


class TestProviderMessages:
    @pytest.mark.parametrize("case", list(_OLLAMA_CASES), ids=list(_OLLAMA_CASES))
    @patch("memtomem.embedding.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_ollama(self, _sleep, case):
        outcome, advice = _OLLAMA_CASES[case]
        embedder = _ollama()
        embedder._client = _mock_client(outcome)

        with pytest.raises(EmbeddingError) as info:
            await embedder.embed_query("q")

        text = _leg_error_text(info.value)
        assert _clean(text), text
        if advice is None:
            assert text.endswith("details are in the server log"), text
        else:
            assert advice in text, text

    @pytest.mark.parametrize("case", list(_OPENAI_CASES), ids=list(_OPENAI_CASES))
    @patch("memtomem.embedding.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_openai(self, _sleep, case):
        outcome, advice = _OPENAI_CASES[case]
        embedder = _openai()
        embedder._client = _mock_client(outcome)

        with pytest.raises(EmbeddingError) as info:
            await embedder.embed_query("q")

        text = _leg_error_text(info.value)
        assert _clean(text), text
        if advice is None:
            assert text.endswith("details are in the server log"), text
        else:
            assert advice in text, text

    async def test_onnx_dependency_advice_survives_the_embed_wrapper(self, monkeypatch):
        from memtomem.embedding import profiles

        def _missing():
            raise missing_onnx_dependency("huggingface_hub")

        monkeypatch.setattr(profiles, "minilm_snapshot", _missing)
        embedder = OnnxEmbedder(
            EmbeddingConfig(
                provider="onnx",
                model="all-MiniLM-L6-v2",
                dimension=384,
                base_url="",
                api_key="",
                max_sequence_tokens=0,
            )
        )
        try:
            with pytest.raises(EmbeddingError) as info:
                await embedder.embed_query("q")
        finally:
            await embedder.close()

        assert "Install memtomem[onnx]" in _leg_error_text(info.value)


class _EndpointEmbedder:
    dimension = 1024
    model_name = "fake"

    async def embed_query(self, query: str) -> list[float]:
        raise RetryableEmbeddingError(f"Cannot connect to Ollama at {_ENDPOINT}")


async def _drain_bg(pipeline) -> None:
    if pipeline._bg_tasks:
        await asyncio.gather(*list(pipeline._bg_tasks), return_exceptions=True)


@pytest.fixture
async def endpoint_dense(components):
    components.search_pipeline._embedder = _EndpointEmbedder()
    yield components
    await _drain_bg(components.search_pipeline)


class TestSurfaces:
    async def test_pipeline_stats_and_web_schema_hold_no_endpoint(self, endpoint_dense):
        _results, stats = await endpoint_dense.search_pipeline.search("nothing matches this")

        assert stats.dense_error is not None and _clean(stats.dense_error)
        web = RetrievalStatsOut(**vars(stats))
        assert web.dense_error == stats.dense_error

    @pytest.mark.parametrize("fmt", ["compact", "verbose", "structured"])
    async def test_mem_search_empty_result_holds_no_endpoint(self, endpoint_dense, fmt):
        ctx = StubCtx(AppContext.from_components(endpoint_dense))

        out = await mem_search(
            query="nothing matches this",
            output_format=fmt,
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert "semantic search unavailable" in out
        assert "a transient failure (RetryableEmbeddingError)" in out
        assert _clean(out)

    @pytest.mark.parametrize("fmt", ["compact", "verbose", "structured"])
    async def test_failed_keyword_leg_holds_no_endpoint(self, components, monkeypatch, fmt):
        import sqlite3

        async def _bm25_down(*_args, **_kwargs):
            raise sqlite3.OperationalError(f"fts unavailable: {_ENDPOINT}")

        monkeypatch.setattr(components.storage, "bm25_search", _bm25_down)
        _results, stats = await components.search_pipeline.search("nothing matches this")
        assert stats.bm25_error == "OperationalError — details are in the server log"
        assert RetrievalStatsOut(**vars(stats)).bm25_error == stats.bm25_error

        ctx = StubCtx(AppContext.from_components(components))
        out = await mem_search(
            query="nothing matches this either",
            output_format=fmt,
            ctx=ctx,  # type: ignore[arg-type]
        )
        await _drain_bg(components.search_pipeline)

        assert "OperationalError — details are in the server log" in out
        assert _clean(out)

    @pytest.mark.parametrize(
        ("outcome", "advice"),
        [
            (httpx.ConnectError(f"refused: {_ENDPOINT}"), "verify 'ollama serve' is running"),
            (_response(404), "ollama pull nomic-embed-text"),
        ],
        ids=["connect", "missing-model"],
    )
    @patch("memtomem.embedding.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_real_ollama_failure_reaches_mem_search_as_advice(
        self, _sleep, components, outcome, advice
    ):
        embedder = _ollama()
        embedder._client = _mock_client(outcome)
        components.search_pipeline._embedder = embedder
        ctx = StubCtx(AppContext.from_components(components))

        out = await mem_search(
            query="nothing matches this",
            output_format="verbose",
            ctx=ctx,  # type: ignore[arg-type]
        )
        await _drain_bg(components.search_pipeline)

        assert advice in out
        assert _clean(out)
