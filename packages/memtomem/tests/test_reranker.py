"""Tests for reranker pipeline components."""

import pytest
from pathlib import Path
from uuid import uuid4
from memtomem.models import Chunk, ChunkMetadata, SearchResult


def _make_result(content, score, rank=1):
    chunk = Chunk(
        content=content,
        metadata=ChunkMetadata(source_file=Path("/tmp/test.md")),
        id=uuid4(),
        embedding=[],
    )
    return SearchResult(chunk=chunk, score=score, rank=rank, source="fused")


class TestCohereReranker:
    def test_init(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.cohere import CohereReranker

        config = RerankConfig(enabled=True, provider="cohere", api_key="test-key")
        reranker = CohereReranker(config)
        assert reranker._config.api_key == "test-key"
        assert reranker._client is None

    @pytest.mark.asyncio
    async def test_empty_results(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.cohere import CohereReranker

        config = RerankConfig(enabled=True, provider="cohere", api_key="test")
        reranker = CohereReranker(config)
        result = await reranker.rerank("query", [], top_k=5)
        assert result == []

    @pytest.mark.asyncio
    async def test_close(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.cohere import CohereReranker

        config = RerankConfig(enabled=True, provider="cohere", api_key="test")
        reranker = CohereReranker(config)
        await reranker.close()
        assert reranker._client is None

    @pytest.mark.asyncio
    async def test_closed_instance_refuses_resurrect(self):
        """#1778: post-close use must raise, not re-create the httpx client —
        a client born after close() on a swapped-out instance leaks."""
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.cohere import CohereReranker

        config = RerankConfig(enabled=True, provider="cohere", api_key="test")
        reranker = CohereReranker(config)
        # Positive control: a live instance builds its client on demand.
        assert reranker._get_client() is not None

        await reranker.close()
        assert reranker._client is None

        with pytest.raises(RuntimeError, match="closed"):
            await reranker.rerank("query", [_make_result("a", 1.0)], top_k=5)
        assert reranker._client is None  # no new client materialized

        await reranker.close()  # idempotent


class TestLocalReranker:
    def test_init(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.local import LocalReranker

        config = RerankConfig(
            enabled=True, provider="local", model="cross-encoder/ms-marco-MiniLM-L-6-v2"
        )
        reranker = LocalReranker(config)
        assert reranker._model is None  # lazy loaded

    @pytest.mark.asyncio
    async def test_empty_results(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.local import LocalReranker

        config = RerankConfig(enabled=True, provider="local")
        reranker = LocalReranker(config)
        result = await reranker.rerank("query", [], top_k=5)
        assert result == []

    @pytest.mark.asyncio
    async def test_close(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.local import LocalReranker

        config = RerankConfig(enabled=True, provider="local")
        reranker = LocalReranker(config)
        await reranker.close()
        assert reranker._model is None

    @pytest.mark.asyncio
    async def test_closed_instance_refuses_resurrect(self):
        """#1778: post-close use must raise, not silently reload the model."""
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.local import LocalReranker

        config = RerankConfig(enabled=True, provider="local")
        reranker = LocalReranker(config)
        # Positive control: a live instance serves its cached model.
        sentinel = object()
        reranker._model = sentinel
        assert reranker._get_model() is sentinel

        await reranker.close()
        assert reranker._model is None

        with pytest.raises(RuntimeError, match="closed"):
            await reranker.rerank("query", [_make_result("a", 1.0)], top_k=5)
        assert reranker._model is None  # no reload

        await reranker.close()  # idempotent


class TestRerankerFactory:
    def test_disabled(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.factory import create_reranker

        assert create_reranker(RerankConfig(enabled=False)) is None

    def test_cohere(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.factory import create_reranker
        from memtomem.search.reranker.cohere import CohereReranker

        r = create_reranker(RerankConfig(enabled=True, provider="cohere"))
        assert isinstance(r, CohereReranker)

    def test_local(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.factory import create_reranker
        from memtomem.search.reranker.local import LocalReranker

        r = create_reranker(RerankConfig(enabled=True, provider="local"))
        assert isinstance(r, LocalReranker)

    def test_unknown_raises(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.factory import create_reranker

        with pytest.raises(ValueError):
            create_reranker(RerankConfig(enabled=True, provider="unknown"))
