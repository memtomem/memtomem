"""The dropped-dense-leg notice travels the real path (#2671).

``run_search`` derives it; these tests prove it reaches the MCP surfaces with
a real pipeline whose embedder raises, and that the exception text — which an
embedder may fill with its endpoint — is not part of the notice, so compact
and structured output with results stay free of it. The verbose pipeline line
and the empty-result branches still carry the raw text (#2675).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from memtomem.server.context import AppContext
from memtomem.server.tools.multi_agent import mem_agent_search
from memtomem.server.tools.search import mem_search
from memtomem.services.search_service import DENSE_LEG_DROPPED_HINT

from helpers import StubCtx, make_chunk

_SECRET = "http://user:hunter2@ollama.internal:11434"


class _FailingEmbedder:
    dimension = 1024
    model_name = "fake"

    async def embed_query(self, query: str) -> list[float]:
        raise RuntimeError(f"Cannot connect to Ollama at {_SECRET}")


async def _drain_bg(pipeline) -> None:
    if pipeline._bg_tasks:
        await asyncio.gather(*list(pipeline._bg_tasks), return_exceptions=True)


@pytest.fixture
async def failing_dense(components):
    storage, pipeline = components.storage, components.search_pipeline
    pipeline._embedder = _FailingEmbedder()
    await storage.upsert_chunks(
        [make_chunk(f"dropped marker body {i}", source=f"d{i}.md") for i in range(3)]
    )
    yield components
    await _drain_bg(pipeline)


class TestMemSearch:
    @pytest.mark.asyncio
    async def test_compact_carries_the_notice_and_not_the_secret(self, failing_dense):
        ctx = StubCtx(AppContext.from_components(failing_dense))

        out = await mem_search(query="dropped marker", ctx=ctx)  # type: ignore[arg-type]

        assert "dropped marker body" in out, "sanity: BM25 still answers"
        assert out.endswith(f"\n\n({DENSE_LEG_DROPPED_HINT})")
        assert _SECRET not in out and "hunter2" not in out

    @pytest.mark.asyncio
    async def test_structured_carries_it_in_hints(self, failing_dense):
        ctx = StubCtx(AppContext.from_components(failing_dense))

        out = await mem_search(
            query="dropped marker",
            output_format="structured",
            ctx=ctx,  # type: ignore[arg-type]
        )

        payload = json.loads(out)
        assert payload["results"]
        assert DENSE_LEG_DROPPED_HINT in payload["hints"]
        assert "hunter2" not in out

    @pytest.mark.asyncio
    async def test_verbose_keeps_the_cause_in_the_pipeline_line(self, failing_dense):
        """The diagnostic channel still names the error; only the notice is fixed."""
        ctx = StubCtx(AppContext.from_components(failing_dense))

        out = await mem_search(
            query="dropped marker",
            output_format="verbose",
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert "Dense-err:Cannot connect to Ollama" in out
        assert f"({DENSE_LEG_DROPPED_HINT})" in out

    @pytest.mark.asyncio
    async def test_a_healthy_search_has_no_notice(self, components):
        """Guards the direction: it is the failure, not the fixture."""

        class _Embedder:
            dimension = 1024
            model_name = "fake"

            async def embed_query(self, query: str) -> list[float]:
                return [0.1] * 1024

        storage, pipeline = components.storage, components.search_pipeline
        pipeline._embedder = _Embedder()
        await storage.upsert_chunks([make_chunk("healthy marker body", source="h.md")])
        ctx = StubCtx(AppContext.from_components(components))

        out = await mem_search(query="healthy marker", ctx=ctx)  # type: ignore[arg-type]
        await _drain_bg(pipeline)

        assert "healthy marker body" in out
        assert "semantic search failed" not in out


class TestMemAgentSearch:
    @pytest.mark.asyncio
    async def test_compact_carries_the_notice(self, failing_dense):
        ctx = StubCtx(AppContext.from_components(failing_dense))

        out = await mem_agent_search(query="dropped marker", ctx=ctx)  # type: ignore[arg-type]

        assert "dropped marker body" in out
        assert f"({DENSE_LEG_DROPPED_HINT})" in out
        assert "hunter2" not in out
