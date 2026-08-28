"""The remaining embedder users count into the component generation (#2199).

#2180 leased the generation from ``SearchPipeline.search`` and the ``IndexEngine``
entry points, which left three users outside the accounting: the dedup scanner's
batch embed, bundle import, and the per-call embeds in ``mem_conflicts`` /
formation. ``revert_to_stored`` could close the embedder under any of them —
rebuilding the scanner on swap only redirects the *next* scan, not one already
running.

What is pinned here is the lease, not the wiring: a scan that entered before a
retirement must keep the retired generation open until it finishes, and an idle
revert must still close inline.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem.generation import ComponentGeneration, hold_app_generation
from memtomem.search.dedup import DedupScanner


def _storage(chunks: list) -> MagicMock:
    """Mock the API ``DedupScanner._get_all_chunks`` actually calls."""
    storage = MagicMock()
    storage.get_all_source_files = AsyncMock(return_value=["a.md"] if chunks else [])
    storage.list_chunks_by_source = AsyncMock(return_value=chunks)
    storage.dense_search = AsyncMock(return_value=[])
    return storage


def _chunk(content: str = "hello") -> MagicMock:
    chunk = MagicMock()
    chunk.content = content
    chunk.content_hash = f"hash-{content}"
    chunk.metadata.project_root = None
    return chunk


class TestDedupScannerHoldsItsGeneration:
    async def test_a_scan_in_flight_defers_the_retired_close(self):
        """The acceptance criterion: a scan that entered before the revert
        finishes on the embedder it started with."""
        gen = ComponentGeneration()
        closed: list[str] = []
        embedding_started, release_embedding = asyncio.Event(), asyncio.Event()

        async def _embed_texts(texts):
            embedding_started.set()
            await release_embedding.wait()
            return [[0.1, 0.2] for _ in texts]

        storage = _storage([_chunk()])
        embedder = MagicMock()
        embedder.embed_texts = _embed_texts

        scanner = DedupScanner(storage=storage, embedder=embedder, generation=gen)
        scan = asyncio.create_task(scanner.scan())
        # A bounded wait: if the scan raises instead of reaching the embedder,
        # this must fail loudly rather than hang on an event nobody will set.
        async with asyncio.timeout(10):
            await embedding_started.wait()

        async def _close() -> None:
            closed.append("embedder")

        # The revert lands mid-scan: leased, so nothing closes yet.
        assert gen.retire(_close) is None
        await asyncio.sleep(0)
        assert closed == [], "the embedder closed under a running scan"

        release_embedding.set()
        async with asyncio.timeout(10):
            await scan
        await asyncio.sleep(0)
        assert closed == ["embedder"], "the retired close never ran after the scan finished"

    async def test_an_idle_revert_still_closes_inline(self):
        """The other half of the acceptance criterion — the lease must not turn
        every retirement into a deferred one."""
        gen = ComponentGeneration()
        storage = _storage([])
        embedder = MagicMock()
        embedder.embed_texts = AsyncMock(return_value=[])

        scanner = DedupScanner(storage=storage, embedder=embedder, generation=gen)
        await scanner.scan()  # finished; the lease is released

        closed: list[str] = []

        async def _close() -> None:
            closed.append("embedder")

        coro = gen.retire(_close)
        assert coro is not None, "an idle generation must hand its close back to the caller"
        await coro
        assert closed == ["embedder"]

    async def test_the_lease_spans_the_whole_scan_not_just_the_embed(self):
        """Holding only around ``embed_texts`` would leave a scan that entered
        before the revert to pick the lease up after the close had run."""
        gen = ComponentGeneration()
        seen_leases: list[int] = []

        async def _get_all_source_files(*_a, **_kw):
            # Phase 0, before the embedder is touched at all.
            seen_leases.append(gen.leases)
            return []

        storage = _storage([])
        storage.get_all_source_files = AsyncMock(side_effect=_get_all_source_files)
        embedder = MagicMock()
        embedder.embed_texts = AsyncMock(return_value=[])

        await DedupScanner(storage=storage, embedder=embedder, generation=gen).scan()
        assert seen_leases == [1], "the scan was unleased before it reached the embedder"

    async def test_an_unwired_scanner_still_works(self):
        """Focused tests and callers with no published generation construct the
        scanner with two arguments; that must stay a no-op lease, not a crash."""
        storage = _storage([])
        embedder = MagicMock()
        embedder.embed_texts = AsyncMock(return_value=[])

        assert await DedupScanner(storage=storage, embedder=embedder).scan() == []


class TestHoldAppGeneration:
    """``hold_app_generation`` is what the tool call sites use."""

    def _ctx(self, components):
        from memtomem.server.context import AppContext

        ctx = AppContext(config=MagicMock())
        ctx._components = components
        return ctx

    async def test_holds_the_published_generation(self):
        gen = ComponentGeneration()
        components = MagicMock()
        components.generation = gen
        ctx = self._ctx(components)

        with hold_app_generation(ctx):
            assert gen.leases == 1
        assert gen.leases == 0

    async def test_a_retired_generation_closes_on_release(self):
        gen = ComponentGeneration()
        components = MagicMock()
        components.generation = gen
        closed: list[str] = []

        async def _close() -> None:
            closed.append("embedder")

        with hold_app_generation(self._ctx(components)):
            assert gen.retire(_close) is None
        await asyncio.sleep(0)
        assert closed == ["embedder"]

    @pytest.mark.parametrize("components", [None, "no-generation"])
    async def test_yields_without_a_generation_to_pin(self, components):
        """Pre-init and generation-less contexts must not have to special-case."""
        if components == "no-generation":
            components = MagicMock()
            components.generation = None
        with hold_app_generation(self._ctx(components)):
            pass

    async def test_a_stand_in_app_yields_without_pretending_to_hold(self):
        """Tests fake the app with whatever the tool needs and nothing more.

        Those must keep working — but an auto-attribute stand-in must not be
        mistaken for a published generation either, or the hold would silently
        do nothing while looking like it worked.
        """
        with hold_app_generation(SimpleNamespace()):
            pass

        components = MagicMock()  # ``components.generation`` auto-creates a mock
        with hold_app_generation(SimpleNamespace(_components=components)):
            pass


class TestToolCallSitesHoldTheGeneration:
    """The holds are only worth anything if the tools actually enter them.

    Driven through the real tool function with the embedder span stubbed, so
    the lease is observed where it matters — inside the call — rather than
    inferred from the source text.
    """

    async def test_mem_conflict_check_is_leased_during_the_embedder_span(self, monkeypatch):
        from memtomem.server.tools import conflict as conflict_mod

        gen = ComponentGeneration()
        components = MagicMock()
        components.generation = gen

        from memtomem.server.context import AppContext

        app = AppContext(config=MagicMock())
        app._components = components

        seen: list[int] = []

        async def _detect_conflicts(*_a, **_kw):
            seen.append(gen.leases)
            return []

        monkeypatch.setattr(
            conflict_mod, "_get_app_initialized", AsyncMock(return_value=app), raising=False
        )
        monkeypatch.setattr(
            "memtomem.search.conflict.detect_conflicts", _detect_conflicts, raising=False
        )
        monkeypatch.setattr(
            "memtomem.server.tools.search._resolve_project_context_root",
            lambda _app: None,
            raising=False,
        )

        await conflict_mod.mem_conflict_check(content="anything", ctx=SimpleNamespace())

        assert seen == [1], "the embedder span ran outside the generation lease"
        assert gen.leases == 0, "the lease outlived the call"

    def _leased_app(self, gen: ComponentGeneration):
        from memtomem.server.context import AppContext

        components = MagicMock()
        components.generation = gen
        app = AppContext(config=MagicMock())
        app._components = components
        return app

    async def test_mem_candidate_evidence_is_leased_during_the_embedder_span(self, monkeypatch):
        from memtomem.server.tools import formation as formation_mod

        gen = ComponentGeneration()
        app = self._leased_app(gen)
        app._components.storage.get_memory_candidate = AsyncMock(return_value=MagicMock())
        seen: list[int] = []

        async def _evidence(*_a, **_kw):
            seen.append(gen.leases)
            return {}

        monkeypatch.setattr(
            formation_mod, "_get_app_initialized", AsyncMock(return_value=app), raising=False
        )
        monkeypatch.setattr(formation_mod, "candidate_neighbour_evidence", _evidence)
        monkeypatch.setattr(
            "memtomem.server.tools.search._resolve_project_context_root",
            lambda _app: None,
            raising=False,
        )

        await formation_mod.mem_candidate_evidence(candidate_id="c1", ctx=SimpleNamespace())

        assert seen == [1], "the embedder span ran outside the generation lease"
        assert gen.leases == 0, "the lease outlived the call"

    async def test_mem_import_is_leased_for_the_whole_bundle(self, monkeypatch, tmp_path):
        from memtomem.server.tools import export_import as import_mod

        gen = ComponentGeneration()
        app = self._leased_app(gen)
        bundle = tmp_path / "bundle.jsonl"
        bundle.write_text("{}\n")
        seen: list[int] = []

        async def _import_chunks(*_a, **_kw):
            seen.append(gen.leases)
            return SimpleNamespace(
                imported_chunks=0,
                updated_chunks=0,
                skipped_chunks=0,
                conflict_skipped_chunks=0,
                failed_chunks=0,
                total_chunks=0,
            )

        monkeypatch.setattr(
            import_mod, "_get_app_initialized", AsyncMock(return_value=app), raising=False
        )
        monkeypatch.setattr(import_mod, "_check_embedding_mismatch", lambda _app: None)
        monkeypatch.setattr(
            "memtomem.tools.export_import.import_chunks", _import_chunks, raising=False
        )

        out = await import_mod.mem_import(input_file=str(bundle), ctx=SimpleNamespace())

        # Assert the call actually succeeded: an incomplete stub would make the
        # tool return an error string through ``@tool_handler`` and the lease
        # assertion below would still pass, which is a false green.
        assert "error" not in out.lower(), out
        assert seen == [1], "the bundle embed ran outside the generation lease"
        assert gen.leases == 0, "the lease outlived the call"


class TestWarmupHoldsTheGeneration:
    """A cold ONNX load is the longest embedder span in the process (#2199)."""

    async def test_warm_models_is_leased(self, monkeypatch):
        from memtomem.server import warmup as warmup_mod

        gen = ComponentGeneration()
        components = MagicMock()
        components.generation = gen
        seen: list[int] = []

        async def _warm_one(component, *_a, **_kw):
            seen.append(gen.leases)
            return warmup_mod.WarmupOutcome(component, "onnx", "m", "loaded")

        monkeypatch.setattr(warmup_mod, "_warm_one", _warm_one)
        await warmup_mod.warm_models(components)

        assert seen == [1, 1], "a model load ran outside the generation lease"
        assert gen.leases == 0

    async def test_a_revert_mid_warmup_does_not_mix_generations(self, monkeypatch):
        """``revert_to_stored`` swaps its new triple onto the *same* ``Components``.

        Reading the reranker after awaiting the embedder load would therefore
        warm the new generation's component while this span leases the retired
        one — the two must not drift apart.
        """
        from memtomem.server import warmup as warmup_mod

        retired, published = ComponentGeneration(), ComponentGeneration()
        old_pipeline, new_pipeline = MagicMock(), MagicMock()
        old_pipeline._reranker, new_pipeline._reranker = "old-reranker", "new-reranker"
        old_embedder = "old-embedder"

        components = MagicMock()
        components.generation = retired
        components.embedder = old_embedder
        components.search_pipeline = old_pipeline

        warmed: list[object] = []

        async def _warm_one(component, _provider, _model, target):
            warmed.append(target)
            if component == "embedder":
                # The revert lands here, mutating the container in place.
                components.generation = published
                components.embedder = "new-embedder"
                components.search_pipeline = new_pipeline
            assert retired.leases == 1, "the span stopped leasing the generation it started on"
            assert published.leases == 0, "the freshly published generation was leased by mistake"
            return warmup_mod.WarmupOutcome(component, "onnx", "m", "loaded")

        monkeypatch.setattr(warmup_mod, "_warm_one", _warm_one)
        await warmup_mod.warm_models(components)

        assert warmed == [old_embedder, "old-reranker"], (
            "warmup crossed generations mid-run: it leased the retired handle but "
            f"warmed {warmed[-1]!r}"
        )
