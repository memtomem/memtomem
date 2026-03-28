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


class TestLocalReranker:
    def test_init(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.local import LocalReranker
        config = RerankConfig(enabled=True, provider="local", model="cross-encoder/ms-marco-MiniLM-L-6-v2")
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
