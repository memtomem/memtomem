"""Tests for the fastembed cross-encoder reranker provider."""

from __future__ import annotations

import pytest

pytest.importorskip(
    "fastembed",
    reason="fastembed not installed — install with `pip install memtomem[onnx]`",
)


def test_factory_wires_fastembed_provider() -> None:
    """create_reranker must route provider='fastembed' to FastEmbedReranker."""
    from memtomem.config import RerankConfig
    from memtomem.search.reranker.factory import create_reranker
    from memtomem.search.reranker.fastembed import FastEmbedReranker

    reranker = create_reranker(
        RerankConfig(
            enabled=True,
            provider="fastembed",
            model="Xenova/ms-marco-MiniLM-L-6-v2",
        )
    )
    assert isinstance(reranker, FastEmbedReranker)


def test_init_does_not_load_model() -> None:
    """FastEmbedReranker must lazy-load (mirrors LocalReranker contract)."""
    from memtomem.config import RerankConfig
    from memtomem.search.reranker.fastembed import FastEmbedReranker

    reranker = FastEmbedReranker(
        RerankConfig(
            enabled=True,
            provider="fastembed",
            model="Xenova/ms-marco-MiniLM-L-6-v2",
        )
    )
    assert reranker._model is None


@pytest.mark.asyncio
async def test_empty_results_skips_model_load() -> None:
    """Empty input must short-circuit before touching the model."""
    from memtomem.config import RerankConfig
    from memtomem.search.reranker.fastembed import FastEmbedReranker

    reranker = FastEmbedReranker(
        RerankConfig(
            enabled=True,
            provider="fastembed",
            model="Xenova/ms-marco-MiniLM-L-6-v2",
        )
    )
    assert await reranker.rerank("query", [], top_k=5) == []
    assert reranker._model is None


@pytest.mark.asyncio
async def test_unknown_model_error_surfaces_supported_hint() -> None:
    """Misconfigured model names must produce a helpful ValueError at rerank
    time — setup/config errors carry actionable hints, they should not be
    swallowed by the graceful-degrade path."""
    from pathlib import Path
    from uuid import uuid4

    from memtomem.config import RerankConfig
    from memtomem.models import Chunk, ChunkMetadata, SearchResult
    from memtomem.search.reranker.fastembed import FastEmbedReranker

    reranker = FastEmbedReranker(
        RerankConfig(
            enabled=True,
            provider="fastembed",
            model="nonexistent/definitely-not-a-real-model",
        )
    )
    chunk = Chunk(
        content="any content",
        metadata=ChunkMetadata(source_file=Path("test.md")),
        id=uuid4(),
        embedding=[],
    )
    candidate = SearchResult(chunk=chunk, score=1.0, rank=1, source="fused")

    with pytest.raises(ValueError) as excinfo:
        await reranker.rerank("query", [candidate], top_k=5)

    msg = str(excinfo.value)
    assert "not supported" in msg
    assert "Xenova/ms-marco-MiniLM-L-6-v2" in msg
    assert "jinaai/jina-reranker-v2-base-multilingual" in msg
    assert "add_custom_model" in msg


async def test_close_releases_model_and_refuses_reuse() -> None:
    """close() releases the cached model AND further use raises (#1778) —
    a closed instance must not silently re-download/re-init the model.
    (Supersedes the pre-#1778 "can be reused" contract, which no caller
    ever relied on.)"""
    from pathlib import Path
    from uuid import uuid4

    from memtomem.config import RerankConfig
    from memtomem.models import Chunk, ChunkMetadata, SearchResult
    from memtomem.search.reranker.fastembed import FastEmbedReranker

    reranker = FastEmbedReranker(
        RerankConfig(
            enabled=True,
            provider="fastembed",
            model="Xenova/ms-marco-MiniLM-L-6-v2",
        )
    )

    class _FakeModel:
        def rerank(self, query, documents):
            return [0.5] * len(documents)

    chunk = Chunk(
        content="any content",
        metadata=ChunkMetadata(source_file=Path("test.md")),
        id=uuid4(),
        embedding=[],
    )
    candidate = SearchResult(chunk=chunk, score=1.0, rank=1, source="fused")

    # Positive control: the same instance reranks successfully pre-close.
    reranker._model = _FakeModel()
    reranked = await reranker.rerank("query", [candidate], top_k=5)
    assert reranked[0].source == "reranked"

    await reranker.close()
    assert reranker._model is None

    with pytest.raises(RuntimeError, match="closed"):
        reranker._get_model()

    # rerank() degrades through its own except-Exception fallback: the
    # original candidates come back untouched, and no model reload happens.
    fallback = await reranker.rerank("query", [candidate], top_k=5)
    assert [r.chunk.id for r in fallback] == [candidate.chunk.id]
    assert fallback[0].source == "fused"
    assert reranker._model is None

    await reranker.close()  # idempotent
