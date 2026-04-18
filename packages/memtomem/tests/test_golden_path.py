"""ONNX golden-path integration test: mem_add -> mem_search -> mem_recall.

Validates the README promise that memtomem works end-to-end with the ONNX
embedder (no external services required) for both English and Korean text,
exercising the hybrid search pipeline (BM25 + dense).

Model choice: ``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2``
(384-dim, ~220 MB, ~50 languages including Korean).  ``bge-m3`` works via
``_register_custom_models_if_needed`` in ``embedding/onnx.py`` but its ONNX
export is ~2.3 GB, so MiniLM-L12 is used here to keep the CI cache small
while still covering multilingual text.

This test complements ``test_user_workflows.py`` (Ollama-gated) by providing
a CI-safe equivalent of the add -> search -> recall round trip.  The fastembed
model is downloaded on first run; CI caches it via ``FASTEMBED_CACHE_PATH``.
"""

from __future__ import annotations

import pytest

from memtomem.tools.memory_writer import append_entry

pytest.importorskip(
    "fastembed",
    reason="fastembed not installed — install with `pip install memtomem[onnx]`",
)

# ``onnx_components`` fixture lives in ``conftest.py`` — shared with
# ``test_multilingual_regression.py``.


class TestOnnxGoldenPath:
    """End-to-end add -> search -> recall flow with ONNX bge-m3."""

    async def test_english_roundtrip_top_1(self, onnx_components):
        """English query returns the matching chunk at rank 1."""
        comp, mem_dir = onnx_components

        target = mem_dir / "english.md"
        append_entry(
            target,
            "Chose Redis for caching due to its low latency and LRU eviction.",
            title="Cache Decision",
        )
        append_entry(
            target,
            "PostgreSQL remains our primary relational database.",
            title="Database Decision",
        )
        stats = await comp.index_engine.index_file(target)
        assert stats.indexed_chunks >= 2

        # mem_search — English query should rank Redis chunk first.
        results, _ = await comp.search_pipeline.search("redis caching", top_k=5)
        assert len(results) >= 1, "Hybrid search returned no results"
        top_contents = [r.chunk.content for r in results[:3]]
        assert "Redis" in results[0].chunk.content, (
            f"Expected Redis chunk at rank 1, got top 3: {top_contents}"
        )

        # mem_recall — both entries must be reachable via recent-chunk listing.
        recall = await comp.storage.recall_chunks(limit=10)
        contents = " ".join(c.content for c in recall)
        assert "Redis" in contents
        assert "PostgreSQL" in contents

    async def test_korean_roundtrip_top_3(self, onnx_components):
        """Korean query finds the matching chunk within top-3.

        Small corpora + multilingual models can exhibit rank flakiness, so we
        assert membership in top-3 rather than top-1 to keep the test stable.
        """
        comp, mem_dir = onnx_components

        target = mem_dir / "korean.md"
        append_entry(
            target,
            "쿠버네티스 클러스터 모니터링 설정을 완료했다.",
            title="모니터링",
        )
        append_entry(
            target,
            "그라파나 대시보드로 메트릭을 시각화한다.",
            title="대시보드",
        )
        stats = await comp.index_engine.index_file(target)
        assert stats.indexed_chunks >= 2

        results, _ = await comp.search_pipeline.search("쿠버네티스 모니터링", top_k=3)
        assert len(results) >= 1, "Hybrid search returned no results for Korean query"
        contents = [r.chunk.content for r in results]
        assert any("쿠버네티스" in c or "모니터링" in c for c in contents), (
            f"Korean content missing from top-3: {[c[:30] for c in contents]}"
        )

        recall = await comp.storage.recall_chunks(limit=10)
        assert len(recall) >= 2
