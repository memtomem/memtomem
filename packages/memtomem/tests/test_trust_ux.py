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

    async def test_recall_emits_hidden_archive_hint(self, trust_components):
        """mem_recall mirrors mem_search: when no namespace is pinned and
        archive chunks exist, the response must include a hidden-count hint."""
        from memtomem.server.tools.recall import mem_recall

        comp, _ = trust_components
        visible = make_chunk("a visible note", namespace="default")
        archived_a = make_chunk("first archived note", namespace="archive:old")
        archived_b = make_chunk("second archived note", namespace="archive:old")
        await comp.storage.upsert_chunks([visible, archived_a, archived_b])

        ctx = _recall_ctx(comp)
        out = await mem_recall(limit=10, ctx=ctx)

        assert "2 memories hidden in system namespaces" in out
        assert 'pass namespace="archive:..." to include them' in out

    async def test_recall_no_hint_when_namespace_pinned(self, trust_components):
        """When the caller pins a namespace, no archive hint is emitted —
        the archive filter never engaged. Mirrors mem_search behaviour."""
        from memtomem.server.tools.recall import mem_recall

        comp, _ = trust_components
        visible = make_chunk("a visible note", namespace="default")
        archived = make_chunk("an archived note", namespace="archive:old")
        await comp.storage.upsert_chunks([visible, archived])

        ctx = _recall_ctx(comp)
        out = await mem_recall(namespace="archive:old", limit=10, ctx=ctx)

        assert "hidden in system namespaces" not in out

    async def test_recall_singular_hint_when_one_archived(self, trust_components):
        """The hint must use 'memory' (singular) when count == 1."""
        from memtomem.server.tools.recall import mem_recall

        comp, _ = trust_components
        archived = make_chunk("only one archived note", namespace="archive:old")
        await comp.storage.upsert_chunks([archived])

        ctx = _recall_ctx(comp)
        out = await mem_recall(limit=10, ctx=ctx)

        assert "1 memory hidden in system namespaces" in out
        assert "1 memories" not in out

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

    async def test_recall_structured_emits_hints_field(self, trust_components):
        """When archive chunks are hidden, structured output must surface the
        hint list alongside the empty result set — mirroring mem_search."""
        from memtomem.server.tools.recall import mem_recall

        comp, _ = trust_components
        visible = make_chunk("a visible note", namespace="default")
        archived_a = make_chunk("archive one", namespace="archive:old")
        archived_b = make_chunk("archive two", namespace="archive:old")
        await comp.storage.upsert_chunks([visible, archived_a, archived_b])

        ctx = _recall_ctx(comp)
        out = await mem_recall(limit=10, output_format="structured", ctx=ctx)
        parsed = json.loads(out)

        assert parsed["kind"] == "recall"
        assert len(parsed["results"]) == 1
        assert parsed["results"][0]["namespace"] == "default"
        # Shared-meaning field names matching _format_structured_results.
        assert set(parsed["results"][0]) == {
            "chunk_id",
            "namespace",
            "source",
            "hierarchy",
            "content",
            "created_at",
            "tags",
        }
        assert parsed["hints"] == [
            '2 memories hidden in system namespaces (pass namespace="archive:..." to include them).'
        ]

    async def test_recall_structured_omits_hints_when_empty(self, trust_components):
        """No archive chunks + no dim mismatch → structured payload has no
        "hints" key (not an empty array) so clients don't render empty UI."""
        from memtomem.server.tools.recall import mem_recall

        comp, _ = trust_components
        visible = make_chunk("only visible note", namespace="default")
        await comp.storage.upsert_chunks([visible])

        ctx = _recall_ctx(comp)
        out = await mem_recall(limit=10, output_format="structured", ctx=ctx)
        parsed = json.loads(out)

        assert parsed["kind"] == "recall"
        assert len(parsed["results"]) == 1
        assert "hints" not in parsed

    async def test_recall_structured_content_untruncated(self, trust_components):
        """Compact recall clips content to 400 chars; structured must not —
        machine consumers need the whole chunk."""
        from memtomem.server.tools.recall import mem_recall

        comp, _ = trust_components
        long_content = "x" * 2000
        chunk = make_chunk(long_content, namespace="default")
        await comp.storage.upsert_chunks([chunk])

        ctx = _recall_ctx(comp)
        out = await mem_recall(limit=10, output_format="structured", ctx=ctx)
        parsed = json.loads(out)

        assert parsed["results"][0]["content"] == long_content

    async def test_recall_structured_returns_json_on_empty(self, trust_components):
        """Structured mode must return parseable JSON even on empty results —
        a machine consumer hitting a zero-hit recall still gets a valid
        payload (with the archive hint if applicable) rather than text.

        mem_search mirrors this behaviour — see
        ``test_search_structured_returns_json_on_empty`` in the sibling class.
        """
        from memtomem.server.tools.recall import mem_recall

        comp, _ = trust_components
        archived = make_chunk("only archived", namespace="archive:old")
        await comp.storage.upsert_chunks([archived])

        ctx = _recall_ctx(comp)
        # Pin a namespace with no chunks → zero results, but archive still
        # hidden from the *default* view; since we pinned, no archive hint.
        out = await mem_recall(
            namespace="nonexistent", limit=10, output_format="structured", ctx=ctx
        )
        parsed = json.loads(out)

        assert parsed["kind"] == "recall"
        assert parsed["results"] == []
        assert "hints" not in parsed  # namespace pinned → archive hint suppressed

    async def test_search_structured_returns_json_on_empty(self, trust_components):
        """mem_search with ``output_format="structured"`` must return a parseable
        JSON payload on empty results — not the plain text "No results found."
        that compact/verbose return. Regression for issue #210.
        """
        from memtomem.server.tools.search import mem_search

        comp, _ = trust_components
        # Seed one chunk so the FTS index is non-empty; query targets a term
        # that won't match so results come back empty via the normal path.
        await comp.storage.upsert_chunks([make_chunk("unrelated note", namespace="default")])

        ctx = _search_ctx(comp)
        out = await mem_search(query="nonexistent-term-xyz", output_format="structured", ctx=ctx)

        parsed = json.loads(out)  # Must not raise JSONDecodeError.
        assert parsed["results"] == []
        # No archive chunks, no dim mismatch, no filter → no hints key.
        assert "hints" not in parsed

    async def test_search_structured_empty_surfaces_archive_hint(self, trust_components):
        """When archive chunks exist but none match the query, the structured
        empty payload must still include the archive-hidden hint so the caller
        knows there are memories they could reach by pinning a namespace."""
        from memtomem.server.tools.search import mem_search

        comp, _ = trust_components
        archived = make_chunk("archived note about pipelines", namespace="archive:old")
        await comp.storage.upsert_chunks([archived])

        ctx = _search_ctx(comp)
        out = await mem_search(query="nonexistent-term-xyz", output_format="structured", ctx=ctx)

        parsed = json.loads(out)
        assert parsed["results"] == []
        assert any("hidden in system namespaces" in h for h in parsed["hints"])

    async def test_search_compact_empty_unchanged(self, trust_components):
        """Compact format must still return the plain-text "No results found."
        message on empty — the structured fix is scoped to structured only."""
        from memtomem.server.tools.search import mem_search

        comp, _ = trust_components
        await comp.storage.upsert_chunks([make_chunk("unrelated note", namespace="default")])

        ctx = _search_ctx(comp)
        out = await mem_search(query="nonexistent-term-xyz", ctx=ctx)

        assert out.startswith("No results found.")

    async def test_recall_invalid_output_format(self, trust_components):
        """Invalid output_format must return an error that names the supported
        values — mem_search accepts 'verbose' but mem_recall intentionally does
        not, so the message helps users unlearn that asymmetry."""
        from memtomem.server.tools.recall import mem_recall

        comp, _ = trust_components
        ctx = _recall_ctx(comp)
        out = await mem_recall(limit=10, output_format="verbose", ctx=ctx)

        assert out.startswith("Error:")
        assert "verbose" in out
        assert "Supported: compact, structured" in out


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

    async def test_recall_announces_dim_mismatch_once(self, trust_components):
        """mem_recall must surface the dim-mismatch notice on first call only,
        sharing the same once-per-session gate as mem_search and mem_add."""
        from memtomem.server.tools.recall import mem_recall

        comp, _ = trust_components
        _install_mismatch(comp.storage)

        ctx = _recall_ctx(comp)
        first = await mem_recall(limit=5, ctx=ctx)
        second = await mem_recall(limit=5, ctx=ctx)

        assert "embedding-reset" in first
        assert "configuration.md#reset-flow" in first
        assert "embedding-reset" not in second  # dedup gate held


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


def _recall_ctx(components):
    """Build an MCP-style ctx wrapping ``trust_components`` for mem_recall.

    The recall tool only touches ``app.config``, ``app.storage``,
    ``app.current_namespace``, ``app._dim_mismatch_announced``, and
    ``app._config_lock``. Everything else can be stubbed cheaply.
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    app = SimpleNamespace(
        config=components.config,
        storage=components.storage,
        current_namespace=None,
        _dim_mismatch_announced=False,
        _config_lock=asyncio.Lock(),
        ensure_initialized=AsyncMock(),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _search_ctx(components):
    """Build an MCP-style ctx wrapping ``trust_components`` for mem_search.

    Adds ``search_pipeline`` (required for the retrieval path) and
    ``webhook_manager=None`` on top of the recall-compatible stub.
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    app = SimpleNamespace(
        config=components.config,
        storage=components.storage,
        search_pipeline=components.search_pipeline,
        current_namespace=None,
        webhook_manager=None,
        _dim_mismatch_announced=False,
        _config_lock=asyncio.Lock(),
        ensure_initialized=AsyncMock(),
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))
