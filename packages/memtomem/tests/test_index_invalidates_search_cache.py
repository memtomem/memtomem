"""Index runs must drop the search result TTL cache (#2141).

``SearchPipeline`` caches ranked results for ``search.cache_ttl`` seconds and
keys them on the query + filters alone — never on content. Every other write
surface calls ``invalidate_cache()``; the indexing surfaces did not, so in a
long-lived process (the MCP server, ``mm web``, ``mm shell``) a query warmed
just before an index run kept answering from the pre-index cache.

The counters cannot answer "did this run write anything?": metadata-only rows
are deliberately reported as ``skipped`` (#2124 tags, #2140 validity) and the
line-range refresh is reported nowhere. So the engine carries an explicit
``IndexingStats.mutated`` flag, and the consumers gate on it.

Three layers:

* engine — ``mutated`` is set exactly when a search-visible row was written;
* watcher — an auto-reindex invalidates, a no-op re-scan does not;
* ``mem_index`` end-to-end — the reported symptom, pinned against the real
  pipeline: re-index then search must see the new tag inside the TTL window.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from helpers import StubCtx
from memtomem.indexing.watcher import FileWatcher
from memtomem.server.context import AppContext
from memtomem.server.tools.indexing import mem_index

BODY = "# Heading alpha\n\nSome searchable content about widgets.\n"


def _mock_embedder(components):
    """Deterministic in-memory embedder (no ONNX), as in the watcher tests."""
    embedder = mock.AsyncMock()
    embedder.embed_texts = mock.AsyncMock(
        side_effect=lambda texts, **_: [[0.1] * 1024 for _ in texts]
    )
    embedder.dimension = 1024
    components.index_engine._embedder = embedder
    return embedder


# ===========================================================================
# Engine: the ``mutated`` signal itself
# ===========================================================================


class TestEngineMutatedFlag:
    async def test_first_index_is_mutated(self, components, memory_dir):
        _mock_embedder(components)
        path = memory_dir / "note.md"
        path.write_text(BODY, encoding="utf-8")

        stats = await components.index_engine.index_file(path)

        assert stats.indexed_chunks > 0
        assert stats.mutated is True

    async def test_steady_state_reindex_is_not_mutated(self, components, memory_dir):
        _mock_embedder(components)
        path = memory_dir / "note.md"
        path.write_text(BODY, encoding="utf-8")
        await components.index_engine.index_file(path)

        stats = await components.index_engine.index_file(path)

        assert stats.indexed_chunks == 0
        assert stats.deleted_chunks == 0
        assert stats.mutated is False

    async def test_tag_only_edit_is_mutated_while_counters_stay_silent(
        self, components, memory_dir
    ):
        """The #2124 case: content hash unchanged, tags rewritten. The counters
        report the chunk as ``skipped``, so only ``mutated`` sees the write —
        and search filters on exactly the column that moved."""
        _mock_embedder(components)
        path = memory_dir / "tagged.md"
        path.write_text("# Heading alpha\n\n> tags: before\n\nBody text.\n", encoding="utf-8")
        await components.index_engine.index_file(path)

        path.write_text("# Heading alpha\n\n> tags: after\n\nBody text.\n", encoding="utf-8")
        stats = await components.index_engine.index_file(path)

        assert stats.indexed_chunks == 0
        assert stats.deleted_chunks == 0
        assert stats.mutated is True

    async def test_line_shift_only_is_mutated(self, components, memory_dir):
        """Hash-identical chunk whose start/end lines moved: nothing is
        re-embedded and no counter moves, but ``line_start``/``line_end`` are
        part of what search returns, so the cache must still drop."""
        _mock_embedder(components)
        path = memory_dir / "shift.md"
        path.write_text("# Heading alpha\n\nBody text.\n", encoding="utf-8")
        await components.index_engine.index_file(path)

        path.write_text("\n\n\n# Heading alpha\n\nBody text.\n", encoding="utf-8")
        stats = await components.index_engine.index_file(path)

        assert stats.mutated is True

    async def test_deleted_file_purge_is_mutated(self, components, memory_dir):
        _mock_embedder(components)
        path = memory_dir / "gone.md"
        path.write_text(BODY, encoding="utf-8")
        await components.index_engine.index_file(path)

        path.unlink()
        stats = await components.index_engine.index_file(path)

        assert stats.deleted_chunks > 0
        assert stats.mutated is True

    async def test_index_path_aggregates_mutated(self, components, memory_dir):
        _mock_embedder(components)
        (memory_dir / "a.md").write_text(BODY, encoding="utf-8")

        first = await components.index_engine.index_path(memory_dir, recursive=True)
        assert first.mutated is True

        second = await components.index_engine.index_path(memory_dir, recursive=True)
        assert second.mutated is False

    async def test_stream_events_carry_mutated(self, components, memory_dir):
        """Per-file ``progress`` events carry it too, not just ``complete``:
        an SSE client can disconnect before the run finishes."""
        _mock_embedder(components)
        (memory_dir / "a.md").write_text(BODY, encoding="utf-8")

        events = [e async for e in components.index_engine.index_path_stream(memory_dir)]

        progress = [e for e in events if e["type"] == "progress"]
        complete = [e for e in events if e["type"] == "complete"]
        assert progress and progress[0]["mutated"] is True
        assert complete and complete[0]["mutated"] is True


# ===========================================================================
# Watcher
# ===========================================================================


class TestWatcherInvalidation:
    def _watcher(self, components):
        counter = {"calls": 0}
        pipeline = SimpleNamespace(
            invalidate_cache=lambda: counter.__setitem__("calls", counter["calls"] + 1)
        )
        watcher = FileWatcher(
            components.index_engine,
            components.config.indexing,
            search_pipeline=pipeline,
        )
        return watcher, counter

    async def test_changed_file_invalidates(self, components, memory_dir):
        _mock_embedder(components)
        path = memory_dir / "watched.md"
        path.write_text(BODY, encoding="utf-8")
        watcher, counter = self._watcher(components)

        await watcher._reindex(path)

        assert counter["calls"] == 1

    async def test_unchanged_file_does_not_invalidate(self, components, memory_dir):
        _mock_embedder(components)
        path = memory_dir / "watched.md"
        path.write_text(BODY, encoding="utf-8")
        await components.index_engine.index_file(path)
        watcher, counter = self._watcher(components)

        await watcher._reindex(path)

        assert counter["calls"] == 0

    async def test_deleted_file_invalidates(self, components, memory_dir):
        _mock_embedder(components)
        path = memory_dir / "watched.md"
        path.write_text(BODY, encoding="utf-8")
        await components.index_engine.index_file(path)
        watcher, counter = self._watcher(components)

        path.unlink()
        await watcher._reindex(path)

        assert counter["calls"] == 1

    async def test_no_pipeline_is_tolerated(self, components, memory_dir):
        """The kwarg is optional — a watcher built without a pipeline (as many
        tests do) must not crash on a mutating reindex."""
        _mock_embedder(components)
        path = memory_dir / "watched.md"
        path.write_text(BODY, encoding="utf-8")
        watcher = FileWatcher(components.index_engine, components.config.indexing)

        await watcher._reindex(path)  # must not raise


# ===========================================================================
# mem_index end-to-end: the reported symptom
# ===========================================================================


@pytest.fixture
async def mcp_app(components, monkeypatch):
    """``AppContext`` over the real component stack with ``invalidate_cache``
    wrapped in a counter — the pattern from ``test_mcp_tag_management_parity``.
    The real method still runs, so the end-to-end assertions below exercise
    the actual cache."""
    app = AppContext.from_components(components)
    counter = {"calls": 0}
    real = components.search_pipeline.invalidate_cache

    def counting_invalidate() -> None:
        counter["calls"] += 1
        real()

    monkeypatch.setattr(components.search_pipeline, "invalidate_cache", counting_invalidate)
    return app, counter


class TestMemIndexEndToEnd:
    async def test_warmed_search_sees_a_tag_rewrite_after_reindex(
        self, mcp_app, components, memory_dir
    ):
        """The issue, pinned: warm the cache with a tag-filtered query, rewrite
        the tag on disk, re-index, and the next search must reflect the new tag
        rather than the pre-index cache entry."""
        app, _ = mcp_app
        _mock_embedder(components)
        pipeline = components.search_pipeline
        path = memory_dir / "note.md"
        path.write_text("# Heading alpha\n\n> tags: before\n\nBody text.\n", encoding="utf-8")
        ctx = StubCtx(app)
        await mem_index(path=str(path), ctx=ctx)

        # Warm the TTL cache on both filters. A non-empty query is required:
        # the filter-only path never populates the cache.
        warmed, _ = await pipeline.search("heading alpha", tag_filter="before")
        assert warmed
        stale, _ = await pipeline.search("heading alpha", tag_filter="after")
        assert not stale

        path.write_text("# Heading alpha\n\n> tags: after\n\nBody text.\n", encoding="utf-8")
        await mem_index(path=str(path), ctx=ctx)

        after, _ = await pipeline.search("heading alpha", tag_filter="after")
        assert after, "re-index did not become visible: stale cached result"
        before, _ = await pipeline.search("heading alpha", tag_filter="before")
        assert not before, "old tag still cached after re-index"

    async def test_steady_state_reindex_does_not_invalidate(self, mcp_app, components, memory_dir):
        """The flag, not the counters, is the gate — and a no-op run stays
        free so a scheduled re-index doesn't churn the cache."""
        app, counter = mcp_app
        _mock_embedder(components)
        path = memory_dir / "note.md"
        path.write_text(BODY, encoding="utf-8")
        ctx = StubCtx(app)

        await mem_index(path=str(path), ctx=ctx)
        assert counter["calls"] == 1

        await mem_index(path=str(path), ctx=ctx)
        assert counter["calls"] == 1
