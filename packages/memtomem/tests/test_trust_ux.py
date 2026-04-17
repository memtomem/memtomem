"""Regression tests for the PR #2 trust-UX surfaces.

Covers the two silent behaviours the core-module review flagged:

* **G2 — archive hint.** Chunks in system namespaces (``archive:*``) are
  excluded from the default namespace=None search. Without a hint, users
  think their memories disappeared. ``SearchPipeline`` now surfaces the
  hidden count via ``RetrievalStats.hidden_system_ns`` and the
  ``mem_search`` formatter append a notice.
* **G3 — embedding dim-mismatch hint.** ``mem_status`` emits a structured
  warning; ``mem_add`` / ``mem_search`` emit a one-shot notice per MCP
  session via ``AppContext._dim_mismatch_announced``.

Plus a component-level smoke flow (status → index → search → add → recall)
so the five hint insertion points do not regress silently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.models import Chunk, ChunkMetadata
from memtomem.server.component_factory import close_components, create_components
from memtomem.server.formatters import _format_structured_results
from memtomem.server.helpers import _announce_dim_mismatch_once, _dim_mismatch_hint
from memtomem.tools.memory_writer import append_entry

from helpers import make_chunk


# ---------------------------------------------------------------------------
# Fixture: a BM25-only component stack with archive:* marked as system-ns.
# ---------------------------------------------------------------------------


@pytest.fixture
async def trust_components(tmp_path, monkeypatch):
    """BM25-only components with ``archive:`` registered as a system namespace.

    No embedder is required — hidden-ns counting and hint flows do not depend
    on dense search. This avoids the bge-m3/ONNX model download cost for
    tests that only care about the trust-UX wiring.
    """
    db_path = tmp_path / "trust.db"
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()

    for var in (
        "MEMTOMEM_EMBEDDING__PROVIDER",
        "MEMTOMEM_EMBEDDING__MODEL",
        "MEMTOMEM_EMBEDDING__DIMENSION",
        "MEMTOMEM_STORAGE__SQLITE_PATH",
        "MEMTOMEM_INDEXING__MEMORY_DIRS",
    ):
        monkeypatch.delenv(var, raising=False)

    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]
    # ``chunks_vec`` is created with ``config.embedding.dimension``; keep it
    # non-zero so upsert_chunks works even though dense search itself is off.
    config.embedding.dimension = 1024
    config.search.enable_dense = False  # BM25 only — no embedder required
    # Keep the default system prefix but spell it out for readability.
    config.search.system_namespace_prefixes = ["archive:"]

    import memtomem.config as _cfg

    monkeypatch.setattr(_cfg, "load_config_overrides", lambda c: None)

    comp = await create_components(config)
    try:
        yield comp, mem_dir
    finally:
        await close_components(comp)


# ---------------------------------------------------------------------------
# G2 — archive hint wiring
# ---------------------------------------------------------------------------


class TestArchiveHint:
    """Hidden system-namespace chunks surface through the pipeline stats."""

    async def test_count_chunks_by_ns_prefix_matches_archive(self, trust_components):
        comp, _ = trust_components

        visible = make_chunk("visible note", namespace="default")
        archived = make_chunk("archived note", namespace="archive:old")
        await comp.storage.upsert_chunks([visible, archived])

        count = await comp.storage.count_chunks_by_ns_prefix(["archive:"])
        assert count == 1

    async def test_pipeline_surfaces_hidden_count_for_global_search(self, trust_components):
        comp, _ = trust_components

        visible = make_chunk("visible note about pipelines", namespace="default")
        archived = make_chunk("archived pipeline notes", namespace="archive:old")
        await comp.storage.upsert_chunks([visible, archived])

        _, stats = await comp.search_pipeline.search("pipeline", top_k=5)
        assert stats.hidden_system_ns == 1

    async def test_pipeline_hidden_count_zero_when_namespace_pinned(self, trust_components):
        """Pinning an explicit namespace bypasses the system-ns filter."""
        comp, _ = trust_components

        visible = make_chunk("visible note", namespace="default")
        archived = make_chunk("archived note", namespace="archive:old")
        await comp.storage.upsert_chunks([visible, archived])

        _, stats = await comp.search_pipeline.search("note", top_k=5, namespace="archive:old")
        # When the caller pins a namespace, the archive isn't being hidden
        # relative to that request — so no hint is warranted.
        assert stats.hidden_system_ns == 0

    def test_structured_formatter_emits_hints_field(self):
        meta = ChunkMetadata(source_file=Path("/tmp/x.md"), namespace="default")
        chunk = Chunk(content="hi", metadata=meta, embedding=[])
        result_cls = type(
            "R",
            (),
            {
                "__init__": lambda self, **k: self.__dict__.update(k),
            },
        )
        r = result_cls(chunk=chunk, score=0.5, rank=1, source="bm25")

        hints = ["3 result(s) hidden in system namespaces."]
        out = _format_structured_results([r], hints=hints)
        parsed = json.loads(out)
        assert parsed["hints"] == hints

        # Backwards compatibility: no hints → no "hints" key.
        out_bare = _format_structured_results([r])
        assert "hints" not in json.loads(out_bare)


# ---------------------------------------------------------------------------
# G3 — dim-mismatch hint wiring
# ---------------------------------------------------------------------------


def _install_mismatch(storage) -> None:
    """Pretend the DB was created with a different embedder than config."""
    storage._dim_mismatch = (384, 1024)
    storage._model_mismatch = ("onnx", "bge-small-en-v1.5", "onnx", "bge-m3")


class TestDimMismatchHint:
    async def test_hint_returns_none_without_mismatch(self, trust_components):
        comp, _ = trust_components
        assert _dim_mismatch_hint(_StubApp(comp.storage, announced=False)) is None

    async def test_hint_contains_reset_pointer(self, trust_components):
        comp, _ = trust_components
        _install_mismatch(comp.storage)
        msg = _dim_mismatch_hint(_StubApp(comp.storage, announced=False))
        assert msg is not None
        assert "embedding-reset" in msg
        assert "configuration.md#reset-flow" in msg

    async def test_announce_only_fires_once(self, trust_components):
        comp, _ = trust_components
        _install_mismatch(comp.storage)
        app = _StubApp(comp.storage, announced=False)

        first = await _announce_dim_mismatch_once(app)
        second = await _announce_dim_mismatch_once(app)

        assert first is not None
        assert "embedding-reset" in first
        assert second is None  # dedup flag blocked the second emission
        assert app._dim_mismatch_announced is True

    async def test_announce_noop_when_no_mismatch(self, trust_components):
        comp, _ = trust_components
        app = _StubApp(comp.storage, announced=False)

        msg = await _announce_dim_mismatch_once(app)
        assert msg is None
        # Flag stays false when nothing was announced — next time a mismatch
        # actually appears it will still be surfaced.
        assert app._dim_mismatch_announced is False


# ---------------------------------------------------------------------------
# Smoke flow — status → index → search → add → recall
# ---------------------------------------------------------------------------


class TestSmokeFlow:
    """Mirrors the ltm-smoke-test critical path so hint wiring regresses loudly.

    Exercises the same component-level calls the MCP tools make, not the
    FastMCP decorators themselves — enough to catch hint/string drift.
    """

    async def test_five_step_roundtrip(self, trust_components):
        comp, mem_dir = trust_components

        # (1) status — get_stats mirrors what mem_status reads.
        stats0 = await comp.storage.get_stats()
        assert stats0["total_chunks"] == 0

        # (2) index — a file full of notes.
        target = mem_dir / "notes.md"
        append_entry(target, "Redis LRU eviction policy for the cache tier.", title="Cache")
        append_entry(target, "Postgres logical replication for the audit log.", title="Audit")
        idx = await comp.index_engine.index_file(target)
        assert idx.indexed_chunks >= 2

        # (3) search — query hits at least one chunk.
        results, stats_search = await comp.search_pipeline.search("Redis cache", top_k=3)
        assert any("Redis" in r.chunk.content for r in results)
        assert stats_search.hidden_system_ns == 0  # no archive chunks yet

        # (4) add — hand-rolled append mirroring mem_add's file step.
        followup = mem_dir / "followup.md"
        append_entry(followup, "New vector store review session scheduled.", title="Review")
        await comp.index_engine.index_file(followup)

        # (5) recall — the new entry shows up in time-ordered recall.
        recall = await comp.storage.recall_chunks(limit=10)
        joined = " ".join(c.content for c in recall)
        assert "vector store review" in joined
        assert "Redis" in joined

        # Now archive something and re-run step (3): the global search should
        # flag the hidden chunk.
        archived = mem_dir / "archive.md"
        append_entry(archived, "Old caching doc preserved for history.", title="Archive")
        await comp.index_engine.index_file(archived, namespace="archive:2024")

        _, stats_after = await comp.search_pipeline.search("caching", top_k=3)
        assert stats_after.hidden_system_ns >= 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubApp:
    """Minimal AppContext surrogate exposing the attributes helpers need."""

    def __init__(self, storage, *, announced: bool) -> None:
        import asyncio

        self.storage = storage
        self._dim_mismatch_announced = announced
        self._config_lock = asyncio.Lock()
