"""Issue #349: MCP server degraded-mode startup on embedding mismatch.

When a DB has ``embedding_dimension=0`` (legacy NoopEmbedder / BM25-only
install) and the runtime config points at a real provider, the server used
to raise ``EmbeddingDimensionMismatchError`` during ``SqliteBackend.initialize``
and die before the MCP handshake — leaving no in-protocol way to repair it.
These tests lock in the recovery-friendly behavior:

* ``create_components`` stays up and exposes ``embedding_broken`` state.
* Vector-dependent writes (``mem_add``, ``mem_batch_add``, ``mem_edit``)
  return an actionable ``_check_embedding_mismatch`` error instead of
  crashing on ``upsert_chunks`` with a missing ``chunks_vec``.
* ``mem_embedding_reset(mode="apply_current")`` is callable from MCP and
  repairs the mismatch end-to-end (``mem_stats`` drops the DEGRADED line,
  ``mem_add`` starts working again).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlite_vec

import memtomem.config as _cfg
from memtomem.config import Mem2MemConfig
from memtomem.server.component_factory import close_components, create_components
from memtomem.server.context import AppContext
from memtomem.server.tools.memory_crud import _mem_add_core
from memtomem.server.tools.status_config import mem_embedding_reset, mem_stats


class _FakeEmbedder:
    """Minimal 1024-d embedder so ``create_components`` does not pull a real model.

    The vectors are deterministic but otherwise meaningless — enough to satisfy
    ``upsert_chunks`` without downloading ONNX weights or talking to Ollama.
    """

    dimension = 1024
    model_name = "bge-m3"

    async def embed_texts(self, texts: Sequence[str], **_kwargs) -> list[list[float]]:
        # ``**_kwargs`` absorbs ``on_progress`` from the EmbeddingProvider Protocol.
        return [[0.0] * 1024 for _ in texts]

    async def embed_query(self, query: str) -> list[float]:
        return [0.0] * 1024

    async def close(self) -> None:
        pass


def _seed_legacy_dim0_db(db_path: Path) -> None:
    """Create a DB that reproduces the issue #349 startup trigger.

    Pre-seeds ``_memtomem_meta`` with ``embedding_dimension=0`` so the next
    ``SqliteBackend.initialize`` with a non-``none`` configured provider trips
    :class:`~memtomem.errors.EmbeddingDimensionMismatchError` unless
    ``strict_dim_check=False``.
    """
    db = sqlite3.connect(str(db_path))
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    try:
        db.execute(
            "CREATE TABLE IF NOT EXISTS _memtomem_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        db.executemany(
            "INSERT OR REPLACE INTO _memtomem_meta(key, value) VALUES (?, ?)",
            [
                ("embedding_dimension", "0"),
                ("embedding_provider", "none"),
                ("embedding_model", ""),
            ],
        )
        db.commit()
    finally:
        db.close()


def _degraded_config(tmp_path, monkeypatch) -> Mem2MemConfig:
    """Config + monkeypatches that put ``create_components`` into degraded mode.

    Seeds a dim=0 DB while the config points at onnx/bge-m3, and stubs the
    embedder factory so nothing downloads ONNX weights.
    """
    db_path = tmp_path / "legacy.db"
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(exist_ok=True)
    _seed_legacy_dim0_db(db_path)

    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]
    config.embedding.provider = "onnx"
    config.embedding.model = "bge-m3"
    config.embedding.dimension = 1024

    monkeypatch.setattr(_cfg, "load_config_overrides", lambda c: None)
    monkeypatch.setattr(_cfg, "load_config_d", lambda c: None)
    monkeypatch.setattr(
        "memtomem.runtime.components.create_embedder",
        lambda embedding_config: _FakeEmbedder(),
    )
    return config


@pytest.fixture
async def degraded_components(tmp_path, monkeypatch):
    """``create_components`` against a dim=0 DB with config pointing at onnx/bge-m3.

    Would have raised ``EmbeddingDimensionMismatchError`` pre-#349; now returns
    ``Components`` with ``embedding_broken`` populated and a relaxed storage.
    """
    comp = await create_components(_degraded_config(tmp_path, monkeypatch))
    try:
        yield comp
    finally:
        await close_components(comp)


@pytest.fixture
async def degraded_app(tmp_path, monkeypatch):
    """A lifespan-owned ``AppContext`` that started in degraded mode (#2181).

    Unlike :func:`_make_app`, this goes through ``ensure_initialized`` — so it
    owns its components and its background loops, which is what
    ``recover_from_degraded`` gates on. The watcher class is stubbed; the tests
    that need a real one patch it themselves.
    """
    from unittest.mock import AsyncMock, MagicMock

    from memtomem.indexing import watcher as watcher_mod

    def _fake_watcher(*_args: object, **_kwargs: object) -> MagicMock:
        fake = MagicMock(name="watcher")
        fake.start = AsyncMock()
        fake.stop = AsyncMock()
        return fake

    monkeypatch.setattr(watcher_mod, "FileWatcher", _fake_watcher)

    app = AppContext(config=_degraded_config(tmp_path, monkeypatch))
    await app.ensure_initialized()
    assert app.embedding_broken is not None, "fixture must start degraded"
    try:
        yield app
    finally:
        await app.close()


class _StubCtx:
    """Minimal stand-in for MCP ``Context`` so tools can be called directly in tests."""

    def __init__(self, app: AppContext) -> None:
        class _RC:
            pass

        self.request_context = _RC()
        self.request_context.lifespan_context = app


def _make_app(components) -> AppContext:
    """Build an ``AppContext`` straight from ``Components`` (no lifespan plumbing).

    Skips watcher / scheduler startup — those would try to touch ``chunks_vec``
    in degraded mode, which is exactly what the lifespan already gates against.
    """
    return AppContext.from_components(components)


async def test_create_components_enters_degraded_instead_of_raising(degraded_components):
    """Pre-#349 this call raised ``EmbeddingDimensionMismatchError``."""
    comp = degraded_components

    assert comp.embedding_broken is not None, "embedding_broken must be populated"
    assert comp.embedding_broken["dimension_mismatch"] is True
    assert comp.embedding_broken["stored"]["dimension"] == 0
    assert comp.embedding_broken["configured"]["dimension"] == 1024
    assert comp.embedding_broken["configured"]["provider"] == "onnx"

    # Live view on the storage must agree — degraded mode is authoritative,
    # not a snapshot, so ``_check_embedding_mismatch`` keeps blocking writes.
    assert comp.storage.embedding_mismatch is not None


async def test_mem_add_blocked_in_degraded_mode(degraded_components):
    """``mem_add`` must return the actionable mismatch error, not crash."""
    app = _make_app(degraded_components)
    ctx = _StubCtx(app)

    message, stats = await _mem_add_core(
        content="hello from a degraded server",
        title=None,
        tags=None,
        file=None,
        namespace=None,
        template=None,
        ctx=ctx,  # type: ignore[arg-type]
        event_type="add",
    )
    assert stats is None
    assert "Embedding mismatch detected" in message
    assert "mm embedding-reset --mode apply-current" in message


async def test_mem_stats_surfaces_degraded_line(degraded_components):
    """Monitoring probes should see the degraded state from mem_stats alone."""
    app = _make_app(degraded_components)
    ctx = _StubCtx(app)

    out = await mem_stats(ctx=ctx)  # type: ignore[arg-type]
    assert "DEGRADED" in out
    assert "mem_embedding_reset" in out


async def test_mem_embedding_reset_apply_current_repairs_mismatch(degraded_components):
    """End-to-end recovery: ``apply_current`` clears the mismatch and ``mem_add`` works."""
    app = _make_app(degraded_components)
    ctx = _StubCtx(app)

    reset_out = await mem_embedding_reset(mode="apply_current", ctx=ctx)  # type: ignore[arg-type]
    assert "onnx/bge-m3" in reset_out
    assert "1024d" in reset_out

    # The receipt must name the forced re-index — an apply-current reset
    # leaves the store with no vectors until it runs (#2115). The CLI is the
    # named remedy because a whole-tree re-embed is a long shell job, not
    # because the MCP call is unsafe: since #2104 both preserve stored
    # namespaces, session or not.
    assert "mm index --force" in reset_out
    assert "keeps the namespace its chunks are stored under" in reset_out
    assert "dense search finds nothing" in reset_out

    # Live storage view: mismatch cleared.
    assert app.storage.embedding_mismatch is None

    # Degraded line should disappear from ``mem_stats`` now that the DB is in sync.
    stats_out = await mem_stats(ctx=ctx)  # type: ignore[arg-type]
    assert "DEGRADED" not in stats_out

    # And ``mem_add`` no longer bounces off the gate (it will actually write
    # through the index engine because chunks_vec was just recreated at 1024d).
    message, add_stats = await _mem_add_core(
        content="post-recovery write sanity check",
        title=None,
        tags=None,
        file=None,
        namespace=None,
        template=None,
        ctx=ctx,  # type: ignore[arg-type]
        event_type="add",
    )
    assert "Embedding mismatch detected" not in message
    assert add_stats is not None
    assert add_stats.indexed_chunks >= 1


async def test_mem_embedding_reset_revert_to_stored_swaps_runtime(degraded_components):
    """Regression for #409: ``revert_to_stored`` mutates ``app._components``
    fields directly (not the read-only ``AppContext`` properties introduced
    by #399 Phase 1). Pre-fix this path raised
    ``AttributeError: property 'embedder' of 'AppContext' object has no setter``
    the moment it ran, defeating the whole recovery flow.

    The degraded fixture pins stored=none/dim=0, configured=onnx/bge-m3/1024,
    so reverting downgrades the runtime to a ``NoopEmbedder`` and clears
    the mismatch. We verify the three runtime slots actually got swapped,
    not just ``embedder`` — a partial fix that touched only ``embedder``
    would leave ``search_pipeline`` / ``index_engine`` holding stale
    references to the configured embedder.
    """
    app = _make_app(degraded_components)
    ctx = _StubCtx(app)
    pre_embedder = app.embedder
    pre_search = app.search_pipeline
    pre_index = app.index_engine

    reset_out = await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

    assert "Reverted to stored DB settings" in reset_out
    assert "none/" in reset_out  # stored provider was "none"
    assert "0d" in reset_out  # stored dimension was 0

    # All three runtime slots swapped. Identity check is the right assertion:
    # construction creates a new instance, so the post object is a different
    # Python object than the pre. Anything narrower (e.g. "dimension == 0")
    # would silently pass if only ``embedder`` was touched and the pipelines
    # kept pointing at the old one.
    assert app.embedder is not pre_embedder
    assert app.search_pipeline is not pre_search
    assert app.index_engine is not pre_index

    # Stored-side settings are now reflected in config + live storage view.
    assert app.config.embedding.provider == "none"
    assert app.config.embedding.dimension == 0
    assert app.storage.embedding_mismatch is None
    assert "DEGRADED" not in await mem_stats(ctx=ctx)  # type: ignore[arg-type]


async def test_revert_to_stored_preserves_llm_on_index_engine(degraded_components):
    """``_revert_to_stored`` rebuilds the index engine so the runtime
    picks up the new (downgraded) embedder. The rebuild must thread
    ``app.llm_provider`` through to the new ``IndexEngine`` — the
    engine consumes it for the per-source AI summary path
    (``maybe_update_ai_summary`` in ``_index_file``). Without explicit
    propagation, the ``llm`` constructor argument silently defaults
    to ``None`` and per-source summarisation stops generating new
    entries until the server is restarted, even though
    ``indexing.auto_summarize`` and ``llm.enabled`` are still on.
    Pin both axes (engine swapped + LLM survives) so a future
    refactor of the rebuild call site can't drop the kwarg
    unnoticed."""
    from unittest.mock import AsyncMock, MagicMock

    comp = degraded_components
    # Inject a sentinel LLM into the live components so we can detect
    # propagation. Real degraded fixture builds with ``llm=None``;
    # poking the field directly mirrors what production does after
    # ``component_factory`` wires in ``create_llm``. ``close()`` must
    # be an ``AsyncMock`` so the fixture's ``close_components`` teardown
    # can ``await comp.llm.close()`` without choking.
    sentinel_llm = MagicMock(name="sentinel_llm")
    sentinel_llm.close = AsyncMock()
    comp.llm = sentinel_llm

    app = _make_app(comp)
    ctx = _StubCtx(app)
    pre_index = app.index_engine

    await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

    assert app.index_engine is not pre_index
    # The engine must still reference the same LLM instance — anything
    # else means the rebuild path silently dropped it.
    assert app.index_engine._llm is sentinel_llm


async def test_revert_to_stored_closes_the_retired_generation(degraded_components):
    """Publish-first, then retire: the swap must close the old pipeline and
    the old embedder, and only after the new generation is published — a
    close that runs before publication would tear resources out from under
    the still-live generation. Pre-fix every revert leaked the retired ONNX
    InferenceSession + its executor thread and the retired pipeline's
    reranker until server restart."""
    app = _make_app(degraded_components)
    ctx = _StubCtx(app)
    pre_embedder = app.embedder
    pre_search = app.search_pipeline
    closed: list[tuple[str, bool, bool]] = []

    def _recording_close(name):
        async def _close():
            # Captured at close time: publication (all three slots swapped)
            # and mismatch clearance must both have happened already.
            closed.append(
                (
                    name,
                    app.embedder is not pre_embedder and app.search_pipeline is not pre_search,
                    app.storage.embedding_mismatch is None,
                )
            )

        return _close

    pre_embedder.close = _recording_close("embedder")
    pre_search.close = _recording_close("pipeline")

    out = await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

    assert "Reverted to stored DB settings" in out
    assert [name for name, _, _ in closed] == ["pipeline", "embedder"]
    assert all(published for _, published, _ in closed)
    assert all(cleared for _, _, cleared in closed)


async def test_a_settled_retirement_is_not_kept_for_shutdown(degraded_components):
    """``retired_generations`` exists so shutdown can close a generation whose
    leaseholder never released. An idle revert closes inline, so by the time
    the call returns there is nothing left to drain — and pre-#2201 the entry
    was still held until the process exited, one per revert."""
    comp = degraded_components
    app = _make_app(comp)
    ctx = _StubCtx(app)

    out = await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

    assert "Reverted to stored DB settings" in out
    assert comp.retired_generations == []


async def test_repeated_reverts_do_not_accumulate_settled_generations(degraded_components):
    """Acceptance criterion 1 of #2201. The mismatch is cleared by each
    revert, so it is re-armed between rounds — the swap path under test is
    the same one a repeatedly-reverting server walks."""
    comp = degraded_components
    app = _make_app(comp)
    ctx = _StubCtx(app)
    storage = comp.storage
    armed = (storage._dim_mismatch, storage._model_mismatch, storage._policy_mismatch)
    assert storage.embedding_mismatch is not None, "fixture must start mismatched"

    for _ in range(3):
        # ``embedding_mismatch`` is derived, so re-arm the three flags the
        # revert clears; setting the property is not possible by design.
        storage._dim_mismatch, storage._model_mismatch, storage._policy_mismatch = armed
        out = await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]
        assert "Reverted to stored DB settings" in out
        assert comp.retired_generations == [], "a settled generation was retained"


async def test_concurrent_reverts_swap_exactly_once(degraded_components):
    """Two racing reverts must not both publish (the loser would close the
    winner's freshly published embedder). Serialized on app._config_lock,
    with the mismatch cleared before the first retirement await, exactly
    one caller reverts and the other reports nothing to do."""
    import asyncio as _asyncio

    app = _make_app(degraded_components)
    ctx = _StubCtx(app)
    release = _asyncio.Event()

    async def _slow_close():
        await release.wait()

    app.search_pipeline.close = _slow_close

    async def _revert():
        return await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

    t1 = _asyncio.create_task(_revert())
    t2 = _asyncio.create_task(_revert())
    await _asyncio.sleep(0.05)
    release.set()
    outs = sorted([await t1, await t2])

    assert sum("Reverted to stored DB settings" in o for o in outs) == 1
    assert sum("No mismatch detected" in o for o in outs) == 1


async def test_revert_cancellation_still_retires_everything(degraded_components):
    """A cancellation during the pipeline close must not skip the embedder
    close (accumulate-and-defer, the lifespan teardown pattern), and the
    mismatch is already cleared in the publication phase."""
    import asyncio as _asyncio
    from unittest.mock import AsyncMock

    from memtomem.server.tools.status_config import _revert_to_stored

    app = _make_app(degraded_components)
    app.search_pipeline.close = AsyncMock(side_effect=_asyncio.CancelledError())
    embedder_close = AsyncMock(name="old_embedder_close")
    app.embedder.close = embedder_close

    with pytest.raises(_asyncio.CancelledError):
        await _revert_to_stored(app)

    embedder_close.assert_awaited_once()
    assert app.storage.embedding_mismatch is None


async def test_revert_to_stored_survives_a_failing_close(degraded_components):
    """A close that fails must not fail the revert: the swap already
    happened, so the recovery the user asked for is done."""
    from unittest.mock import AsyncMock

    app = _make_app(degraded_components)
    ctx = _StubCtx(app)
    app.embedder.close = AsyncMock(side_effect=RuntimeError("close failure"))

    out = await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

    assert "Reverted to stored DB settings" in out
    assert app.storage.embedding_mismatch is None


async def test_a_stored_provider_the_factory_rejects_leaves_the_runtime_untouched(
    tmp_path, monkeypatch
):
    """#2421: the stored identity used to be copied into the live config and
    the storage's policy fields before ``create_embedder`` saw it. A stamp this
    binary cannot build — here a provider from a newer release — then raised
    with ``app.config`` naming that provider while the old embedder, pipeline
    and engine stayed published, and the mismatch was still reported."""
    from memtomem.errors import ConfigError
    from memtomem.server.tools.status_config import _revert_to_stored

    config = _degraded_config(tmp_path, monkeypatch)
    db = sqlite3.connect(str(config.storage.sqlite_path))
    try:
        db.executemany(
            "INSERT OR REPLACE INTO _memtomem_meta(key, value) VALUES (?, ?)",
            [
                ("embedding_provider", "provider-from-a-newer-release"),
                ("embedding_model", "future-model"),
                ("embedding_policy_fingerprint", "stored-policy"),
                ("embedding_max_sequence_tokens", "77"),
            ],
        )
        db.commit()
    finally:
        db.close()
    comp = await create_components(config)
    try:
        app = _make_app(comp)
        mismatch = app.storage.embedding_mismatch
        assert mismatch is not None
        assert mismatch["stored"]["provider"] == "provider-from-a-newer-release"
        assert mismatch["stored"]["max_sequence_tokens"] == 77

        # Identity too: providers hold this object by reference, so restoring
        # the values onto a replacement object would strand them.
        embedding_object = app.config.embedding
        embedding_before = embedding_object.model_dump()
        storage = app.storage
        policy_before = (
            storage._embedding_policy_fingerprint,
            storage._embedding_max_sequence_tokens,
        )
        assert policy_before != ("stored-policy", 77), "fixture must make a rollback observable"
        published_before = (
            comp.embedder,
            comp.search_pipeline,
            comp.index_engine,
            comp.generation,
        )

        with pytest.raises(ConfigError, match="provider-from-a-newer-release"):
            await _revert_to_stored(app)

        assert app.config.embedding is embedding_object
        assert app.config.embedding.model_dump() == embedding_before
        assert (
            storage._embedding_policy_fingerprint,
            storage._embedding_max_sequence_tokens,
        ) == policy_before
        assert (
            comp.embedder,
            comp.search_pipeline,
            comp.index_engine,
            comp.generation,
        ) == published_before
        assert app.storage.embedding_mismatch == mismatch
    finally:
        await close_components(comp)


def _restamp_db(config: Mem2MemConfig, **section: object) -> None:
    """Stamp the fixture DB with the identity and policy *section* would write."""
    from memtomem.config import EmbeddingConfig, embedding_policy_fingerprint

    stamped = EmbeddingConfig(**section)
    db = sqlite3.connect(str(config.storage.sqlite_path))
    try:
        db.executemany(
            "INSERT OR REPLACE INTO _memtomem_meta(key, value) VALUES (?, ?)",
            [
                ("embedding_dimension", str(stamped.dimension)),
                ("embedding_provider", stamped.provider),
                ("embedding_model", stamped.model),
                ("embedding_policy_fingerprint", embedding_policy_fingerprint(stamped)),
                ("embedding_max_sequence_tokens", str(stamped.max_sequence_tokens)),
            ],
        )
        db.commit()
    finally:
        db.close()


async def _revert_recording_embedder_config(app: AppContext, **overrides: object) -> list:
    """Run the revert with a stub factory that records the section it is given."""
    from memtomem.indexing.engine import IndexEngine
    from memtomem.runtime.components import create_search_pipeline
    from memtomem.search.dedup import DedupScanner
    from memtomem.server.tools.status_config import _revert_to_stored_locked

    seen: list = []

    def _recording_factory(section):
        seen.append((section is app.config.embedding, section.threads, section.onnx_batch_size))
        return _FakeEmbedder()

    constructors = {
        "IndexEngine": IndexEngine,
        "DedupScanner": DedupScanner,
        "SearchPipeline": create_search_pipeline,
        **overrides,
    }
    await _revert_to_stored_locked(
        app,
        _recording_factory,
        constructors["IndexEngine"],
        constructors["DedupScanner"],
        constructors["SearchPipeline"],
    )
    return seen


@pytest.fixture
def budget_checks(monkeypatch) -> list:
    """Record chunk-budget validation instead of running it.

    For E5 it resolves the pinned chunk tokenizer, which downloads on a cold
    cache. Records ``(model, chunk_model_tokens)`` per call.
    """
    from memtomem.chunking import bounded

    calls: list = []

    def _record(config, previous=None) -> None:
        calls.append((config.embedding.model, config.indexing.chunk_model_tokens))

    monkeypatch.setattr(bounded, "validate_budget_configuration", _record)
    return calls


_E5_STAMP = {"provider": "onnx", "model": "intfloat/multilingual-e5-small"}
_BGE_STAMP = {"provider": "onnx", "model": "BAAI/bge-m3", "dimension": 1024}


async def test_revert_to_e5_applies_the_e5_cpu_profile(tmp_path, monkeypatch, budget_checks):
    """#2609: reverting a bge-m3 config to an E5 store used to assign the
    identity and keep bge-m3's generated threads=4 / onnx_batch_size=8. The
    embedder must be built from the live section with E5's 2 / 4."""
    config = _degraded_config(tmp_path, monkeypatch)
    assert (config.embedding.threads, config.embedding.onnx_batch_size) == (4, 8)
    _restamp_db(config, **_E5_STAMP)
    comp = await create_components(config)
    try:
        from memtomem.embedding.profiles import E5_TOKENIZER, PROFILE_INDEXING_FIELDS

        app = _make_app(comp)
        embedding_object = app.config.embedding
        indexing_object = app.config.indexing
        memory_dirs_object = indexing_object.memory_dirs
        assert indexing_object.chunk_model_tokens == 8192
        # Storage starts on the live section by reference; a config hot reload
        # leaves it holding a copy, which the revert must replace as well.
        await app.storage.configure_chunk_budget(indexing_object)
        assert app.storage._chunk_budget_config is not indexing_object

        seen = await _revert_recording_embedder_config(app)

        assert seen == [(True, 2, 4)]
        assert app.config.embedding is embedding_object
        assert app.config.embedding.model == "intfloat/multilingual-e5-small"
        assert (app.config.embedding.dimension, app.config.embedding.max_sequence_tokens) == (
            384,
            512,
        )
        # Regenerated, not pinned: a later profile change can still replace them.
        assert {"threads", "onnx_batch_size"}.isdisjoint(app.config.embedding.model_fields_set)
        assert app.storage.embedding_mismatch is None
        # The indexing budget follows the stored model too: bge-m3's generic
        # 8192-token chunks under E5's 512-token cap are refused at startup.
        indexing = app.config.indexing
        assert indexing is indexing_object
        assert indexing.memory_dirs is memory_dirs_object
        assert (indexing.hard_max_chunk_tokens, indexing.chunk_model_tokens) == (384, 512)
        assert (indexing.chunk_tokenizer_path, indexing.chunk_input_prefix) == (
            E5_TOKENIZER,
            "passage: ",
        )
        assert set(PROFILE_INDEXING_FIELDS).isdisjoint(indexing.model_fields_set)
        assert budget_checks[-1] == ("intfloat/multilingual-e5-small", 512)
        assert app.storage._chunk_budget_config.chunk_model_tokens == 512
    finally:
        await close_components(comp)


async def test_revert_to_e5_keeps_explicit_threads_and_batch(tmp_path, monkeypatch, budget_checks):
    config = _degraded_config(tmp_path, monkeypatch)
    config.embedding.threads = 3
    config.embedding.onnx_batch_size = 16
    _restamp_db(config, **_E5_STAMP)
    comp = await create_components(config)
    try:
        app = _make_app(comp)

        seen = await _revert_recording_embedder_config(app)

        assert seen == [(True, 3, 16)]
        assert {"threads", "onnx_batch_size"} <= app.config.embedding.model_fields_set
    finally:
        await close_components(comp)


async def test_revert_from_e5_to_bge_m3_drops_the_e5_profile(tmp_path, monkeypatch, budget_checks):
    from memtomem.config import EmbeddingConfig

    config = _degraded_config(tmp_path, monkeypatch)
    config.embedding = EmbeddingConfig(provider="onnx", model="multilingual-e5-small")
    assert (config.embedding.threads, config.embedding.onnx_batch_size) == (2, 4)
    _restamp_db(config, **_BGE_STAMP)
    comp = await create_components(config)
    try:
        app = _make_app(comp)

        seen = await _revert_recording_embedder_config(app)

        assert seen == [(True, 4, 8)]
        assert (app.config.embedding.model, app.config.embedding.dimension) == (
            "BAAI/bge-m3",
            1024,
        )
        assert (
            app.config.indexing.hard_max_chunk_tokens,
            app.config.indexing.chunk_model_tokens,
        ) == (0, 8192)
    finally:
        await close_components(comp)


async def test_failed_revert_to_e5_restores_the_profile_and_explicit_fields(
    tmp_path, monkeypatch, budget_checks
):
    """The rollback must undo the rebuilt profile, and it must leave the
    generated fields unmarked: restoring values by assignment alone marks
    them explicit, so a later profile change could no longer replace them."""

    config = _degraded_config(tmp_path, monkeypatch)
    _restamp_db(config, **_E5_STAMP)
    comp = await create_components(config)
    try:
        app = _make_app(comp)
        embedding_object = app.config.embedding
        dump_before = embedding_object.model_dump()
        fields_before = set(embedding_object.model_fields_set)
        assert "threads" not in fields_before
        indexing_before = app.config.indexing.model_dump()
        indexing_fields_before = set(app.config.indexing.model_fields_set)
        budget_before = app.storage._chunk_budget_config

        def _raising(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("injected IndexEngine failure")

        with pytest.raises(RuntimeError, match="injected IndexEngine failure"):
            await _revert_recording_embedder_config(app, IndexEngine=_raising)

        assert app.config.embedding is embedding_object
        assert embedding_object.model_dump() == dump_before
        assert set(embedding_object.model_fields_set) == fields_before
        assert app.config.indexing.model_dump() == indexing_before
        assert app.config.indexing.chunk_model_tokens == 8192, "fixture must move the budget"
        assert set(app.config.indexing.model_fields_set) == indexing_fields_before
        assert app.storage._chunk_budget_config is budget_before
        assert app.storage.embedding_mismatch is not None
    finally:
        await close_components(comp)


async def test_revert_refused_by_the_validator_changes_nothing(tmp_path, monkeypatch):
    """An explicit quantized variant cannot sit under a MiniLM stamp. The
    rebuild refuses before the live section or storage is touched."""
    config = _degraded_config(tmp_path, monkeypatch)
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    # The policy fingerprint hashes the manifest; its content is not read here.
    (artifact / "manifest.json").write_text("{}", encoding="utf-8")
    config.embedding.onnx_variant = "int8-arm64"
    config.embedding.onnx_artifact_path = str(artifact)
    _restamp_db(config, provider="onnx", model="all-MiniLM-L6-v2", dimension=384)
    comp = await create_components(config)
    try:
        app = _make_app(comp)
        watcher = MagicMock(name="watcher")
        app._watcher = watcher
        before = _revert_visible_state(app)
        fields_before = set(app.config.embedding.model_fields_set)

        with pytest.raises(ValueError, match="Nothing was changed") as raised:
            await _revert_recording_embedder_config(app)

        assert "quantized CPU profiles support" in str(raised.value)
        _assert_revert_state_unchanged(app, before)
        assert set(app.config.embedding.model_fields_set) == fields_before
        watcher.rebind.assert_not_called()
    finally:
        await close_components(comp)


async def test_revert_refuses_when_a_config_edit_lands_during_its_budget_check(
    tmp_path, monkeypatch
):
    """``mem_config`` edits sections without ``_config_lock``. An edit that
    lands while the revert awaits its budget check must survive: the revert
    refuses instead of adopting candidates built from the older sections."""
    from memtomem.chunking import bounded

    config = _degraded_config(tmp_path, monkeypatch)
    _restamp_db(config, **_E5_STAMP)
    comp = await create_components(config)
    try:
        app = _make_app(comp)
        watcher = MagicMock(name="watcher")
        app._watcher = watcher

        def _edit_during_check(candidate, previous=None) -> None:
            # What ``mem_config`` does, landing inside the revert's await.
            app.config.embedding.onnx_batch_size = 16

        monkeypatch.setattr(bounded, "validate_budget_configuration", _edit_during_check)

        with pytest.raises(ValueError, match="configuration changed while") as raised:
            await _revert_recording_embedder_config(app)

        assert "Nothing was changed" in str(raised.value)
        assert app.config.embedding.onnx_batch_size == 16
        assert app.config.embedding.model == "bge-m3"
        assert app.storage.embedding_mismatch is not None
        watcher.rebind.assert_not_called()
    finally:
        await close_components(comp)


async def test_revert_refuses_an_explicit_budget_the_stored_model_rejects(tmp_path, monkeypatch):
    """An explicit ``chunk_model_tokens=8192`` fits bge-m3 but not E5; the
    revert refuses before touching the running config, as startup would."""
    config = _degraded_config(tmp_path, monkeypatch)
    config.indexing.chunk_model_tokens = 8192
    _restamp_db(config, **_E5_STAMP)
    comp = await create_components(config)
    try:
        app = _make_app(comp)
        before = _revert_visible_state(app)
        indexing_before = app.config.indexing.model_dump()

        with pytest.raises(ValueError, match="E5 requires exact chunk budgets"):
            await _revert_recording_embedder_config(app)

        _assert_revert_state_unchanged(app, before)
        assert app.config.indexing.model_dump() == indexing_before
    finally:
        await close_components(comp)


def _artifact(tmp_path: Path, name: str = "artifact") -> Path:
    directory = tmp_path / name
    directory.mkdir()
    # The policy fingerprint hashes the manifest; its content is not read here.
    (directory / "manifest.json").write_text("{}", encoding="utf-8")
    return directory


async def _seed_store(config: Mem2MemConfig, *, vectors: int, **section: object) -> None:
    """Stamp *section* on the fixture DB and store *vectors* vectors built under it."""
    from helpers import make_chunk
    from memtomem.config import EmbeddingConfig, StorageConfig, embedding_policy_fingerprint
    from memtomem.storage.sqlite_backend import SqliteBackend

    _restamp_db(config, **section)
    stamped = EmbeddingConfig(**section)
    storage = SqliteBackend(
        StorageConfig(sqlite_path=config.storage.sqlite_path),
        dimension=stamped.dimension,
        embedding_provider=stamped.provider,
        embedding_model=stamped.model,
        embedding_policy_fingerprint=embedding_policy_fingerprint(stamped),
        embedding_max_sequence_tokens=stamped.max_sequence_tokens,
    )
    await storage.initialize()
    try:
        if vectors:
            vector = [1.0] + [0.0] * (stamped.dimension - 1)
            await storage.upsert_chunks(
                [make_chunk(f"stored {n}", embedding=vector) for n in range(vectors)]
            )
    finally:
        await storage.close()


def _set_meta(config: Mem2MemConfig, **rows: str | None) -> None:
    """Write raw meta rows; ``None`` deletes the row."""
    db = sqlite3.connect(str(config.storage.sqlite_path))
    try:
        for key, value in rows.items():
            if value is None:
                db.execute("DELETE FROM _memtomem_meta WHERE key=?", (key,))
            else:
                db.execute(
                    "INSERT OR REPLACE INTO _memtomem_meta(key, value) VALUES (?, ?)",
                    (key, value),
                )
        db.commit()
    finally:
        db.close()


def _stored_meta(config: Mem2MemConfig, key: str) -> str | None:
    db = sqlite3.connect(str(config.storage.sqlite_path))
    try:
        row = db.execute("SELECT value FROM _memtomem_meta WHERE key=?", (key,)).fetchone()
    finally:
        db.close()
    return row[0] if row else None


def _keep_quantized(config: Mem2MemConfig, artifact: Path) -> None:
    config.embedding.onnx_variant = "int8-arm64"
    config.embedding.onnx_artifact_path = str(artifact)


async def _assert_revert_refused(config: Mem2MemConfig, *fragments: str) -> None:
    """Revert must refuse before anything changes, in memory or on disk."""
    comp = await create_components(config)
    # Read after the open, which may backfill it.
    policy_row = _stored_meta(config, "embedding_policy_fingerprint")
    try:
        app = _make_app(comp)
        watcher = MagicMock(name="watcher")
        app._watcher = watcher
        before = _revert_visible_state(app)
        assert before["mismatch"] is not None, "fixture must start degraded"

        with pytest.raises(ValueError, match="Nothing was changed") as raised:
            await _revert_recording_embedder_config(app)

        for fragment in fragments:
            assert fragment in str(raised.value)
        _assert_revert_state_unchanged(app, before)
        watcher.rebind.assert_not_called()
    finally:
        await close_components(comp)
    assert _stored_meta(config, "embedding_policy_fingerprint") == policy_row


async def test_revert_refuses_a_quantized_variant_onto_a_populated_fp32_store(
    tmp_path, monkeypatch, budget_checks
):
    """#2617: a quantized bge-m3 config reverted onto an fp32 E5 store kept its
    variant and ran quantized E5 over fp32 vectors, clearing the mismatch."""
    config = _degraded_config(tmp_path, monkeypatch)
    _keep_quantized(config, _artifact(tmp_path))
    await _seed_store(config, vectors=1, **_E5_STAMP)

    await _assert_revert_refused(config, "keeps onnx_variant='int8-arm64'", "onnx_variant='fp32'")


async def test_revert_refuses_fp32_onto_a_populated_store_stamped_quantized(
    tmp_path, monkeypatch, budget_checks
):
    config = _degraded_config(tmp_path, monkeypatch)
    artifact = _artifact(tmp_path)
    await _seed_store(
        config,
        vectors=1,
        **_E5_STAMP,
        onnx_variant="int8-arm64",
        onnx_artifact_path=str(artifact),
    )

    await _assert_revert_refused(config, "built with onnx_variant='int8-arm64'", "keeps fp32")


@pytest.mark.parametrize("quantized", [True, False], ids=["quantized", "fp32"])
async def test_a_populated_onnx_store_backfilled_as_none(
    tmp_path, monkeypatch, budget_checks, quantized
):
    """A populated ONNX store once opened under ``provider=none`` carries the
    ``none:v1`` backfill. That is not a quantized policy, so it is fp32."""
    config = _degraded_config(tmp_path, monkeypatch)
    if quantized:
        _keep_quantized(config, _artifact(tmp_path))
    await _seed_store(config, vectors=1, **_E5_STAMP)
    _set_meta(config, embedding_policy_fingerprint="none:v1")

    if quantized:
        await _assert_revert_refused(config, "stored policy 'none:v1'")
        return
    comp = await create_components(config)
    try:
        app = _make_app(comp)
        await _revert_recording_embedder_config(app)
        assert app.storage.embedding_mismatch is None
        # A populated store keeps its row: vectors exist to be described by it.
        assert _stored_meta(config, "embedding_policy_fingerprint") == "none:v1"
    finally:
        await close_components(comp)


async def test_revert_allows_the_variant_the_store_was_built_with(
    tmp_path, monkeypatch, budget_checks
):
    from memtomem.config import EmbeddingConfig, embedding_policy_fingerprint

    config = _degraded_config(tmp_path, monkeypatch)
    artifact = _artifact(tmp_path)
    _keep_quantized(config, artifact)
    section = {**_E5_STAMP, "onnx_variant": "int8-arm64", "onnx_artifact_path": str(artifact)}
    await _seed_store(config, vectors=1, **section)
    comp = await create_components(config)
    try:
        app = _make_app(comp)

        await _revert_recording_embedder_config(app)

        assert app.storage.embedding_mismatch is None
        assert app.config.embedding.model == "intfloat/multilingual-e5-small"
        assert app.config.embedding.onnx_variant == "int8-arm64"
        assert _stored_meta(config, "embedding_policy_fingerprint") == (
            embedding_policy_fingerprint(EmbeddingConfig(**section))
        )
    finally:
        await close_components(comp)


async def _empty_backfilled_bge_store(tmp_path: Path, monkeypatch) -> tuple[Mem2MemConfig, Path]:
    """An empty bge-m3 store whose policy row the quantized opener backfills.

    ``chunks_vec`` exists but holds no rows, and the policy row is missing, as
    in a store created before policies were recorded. Storage init keys the
    legacy backfill on the table existing, not on it holding vectors.
    """
    config = _degraded_config(tmp_path, monkeypatch)
    artifact = _artifact(tmp_path)
    _keep_quantized(config, artifact)
    await _seed_store(config, vectors=0, **_BGE_STAMP)
    _set_meta(config, embedding_policy_fingerprint=None, embedding_max_sequence_tokens=None)
    return config, artifact


async def test_revert_onto_an_empty_store_ignores_the_backfilled_policy(
    tmp_path, monkeypatch, budget_checks
):
    """#2617 acceptance: the backfilled row says nothing about vectors, since
    there are none, so it must not refuse the revert."""
    from memtomem.config import EmbeddingConfig, StorageConfig, embedding_policy_fingerprint
    from memtomem.embedding.profiles import variant_identity
    from memtomem.storage.sqlite_backend import SqliteBackend

    config, artifact = await _empty_backfilled_bge_store(tmp_path, monkeypatch)
    comp = await create_components(config)
    try:
        app = _make_app(comp)
        storage = app.storage
        # The real backfill, not a seeded string.
        assert _stored_meta(config, "embedding_policy_fingerprint") == (
            "onnx:v1:max_sequence_tokens=0"
        )
        assert storage.embedding_mismatch is not None

        await _revert_recording_embedder_config(app)

        assert storage.embedding_mismatch is None
        reverted = app.config.embedding
        assert reverted.onnx_variant == "int8-arm64"
        # The store now records the policy the revert runs, so the vectors it
        # indexes next are described by it (round-1 review of #2617).
        expected = embedding_policy_fingerprint(reverted)
        assert expected.endswith(f":int8-arm64:{variant_identity(str(artifact))}")
        assert _stored_meta(config, "embedding_policy_fingerprint") == expected
        assert _stored_meta(config, "embedding_max_sequence_tokens") == str(
            reverted.max_sequence_tokens
        )
        assert storage._embedding_policy_fingerprint == expected
        assert storage._embedding_max_sequence_tokens == reverted.max_sequence_tokens
        await storage.upsert_chunks([_chunk_1024("indexed after the revert")])
        assert (await storage.read_embedding_stamp_fresh())["vectors"] == 1
        section = EmbeddingConfig.model_validate(reverted.model_dump())
    finally:
        await close_components(comp)

    reopened = SqliteBackend(
        StorageConfig(sqlite_path=config.storage.sqlite_path),
        dimension=section.dimension,
        embedding_provider=section.provider,
        embedding_model=section.model,
        embedding_policy_fingerprint=embedding_policy_fingerprint(section),
        embedding_max_sequence_tokens=section.max_sequence_tokens,
    )
    await reopened.initialize()
    try:
        assert reopened.embedding_mismatch is None
    finally:
        await reopened.close()


def _chunk_1024(content: str):
    from helpers import make_chunk

    return make_chunk(content, embedding=[1.0] + [0.0] * 1023)


async def test_revert_checks_the_policy_on_disk_not_the_one_read_at_open(
    tmp_path, monkeypatch, budget_checks
):
    """PR #2621 review: another server reverted this empty fp32 E5 store to
    quantized E5 and indexed, after this server opened it. The variant check
    must read that policy from the file, not the fp32 one cached at open."""
    from memtomem.config import EmbeddingConfig, embedding_policy_fingerprint

    config = _degraded_config(tmp_path, monkeypatch)
    artifact = _artifact(tmp_path)
    await _seed_store(config, vectors=0, **_E5_STAMP)
    comp = await create_components(config)
    try:
        app = _make_app(comp)
        cached = app.storage.embedding_mismatch["stored"]["policy_fingerprint"]
        assert ":int8-arm64:" not in cached
        # The other server: the E5 policy it adopted, and one vector under it.
        quantized = EmbeddingConfig(
            **_E5_STAMP, onnx_variant="int8-arm64", onnx_artifact_path=str(artifact)
        )
        _set_meta(config, embedding_policy_fingerprint=embedding_policy_fingerprint(quantized))
        other = sqlite3.connect(str(config.storage.sqlite_path))
        other.enable_load_extension(True)
        sqlite_vec.load(other)
        try:
            other.execute(
                "INSERT INTO chunks_vec(rowid, embedding) VALUES (1, ?)",
                (sqlite_vec.serialize_float32([1.0] + [0.0] * 383),),
            )
            other.commit()
        finally:
            other.close()
        watcher = MagicMock(name="watcher")
        app._watcher = watcher
        before = _revert_visible_state(app)

        with pytest.raises(ValueError, match="Nothing was changed") as raised:
            await _revert_recording_embedder_config(app)

        assert "onnx_variant='int8-arm64'" in str(raised.value)
        assert "keeps fp32" in str(raised.value)
        _assert_revert_state_unchanged(app, before)
        watcher.rebind.assert_not_called()
    finally:
        await close_components(comp)


async def test_revert_refuses_when_another_process_restamped_the_identity(
    tmp_path, monkeypatch, budget_checks
):
    """The identity this server read at open is what it would revert to; if
    the file now records another model, reverting to the old one is wrong."""
    config = _degraded_config(tmp_path, monkeypatch)
    await _seed_store(config, vectors=0, **_E5_STAMP)
    comp = await create_components(config)
    try:
        app = _make_app(comp)
        assert "e5" in app.storage.embedding_mismatch["stored"]["model"].lower()
        _set_meta(config, embedding_model="all-MiniLM-L6-v2")

        with pytest.raises(ValueError, match="Nothing was changed") as raised:
            await _revert_recording_embedder_config(app)

        assert "all-MiniLM-L6-v2" in str(raised.value)
        assert "restart the server" in str(raised.value)
        assert app.storage.embedding_mismatch is not None
    finally:
        await close_components(comp)


async def test_revert_refuses_when_the_stamp_moves_before_it_publishes(
    tmp_path, monkeypatch, budget_checks
):
    """The checks run on a read that is not a snapshot. The last step re-reads
    under the write lock: here the first read missed the store's vector, so
    the revert planned to adopt a quantized policy over fp32 vectors."""
    from memtomem.storage.sqlite_backend import SqliteBackend

    config = _degraded_config(tmp_path, monkeypatch)
    _keep_quantized(config, _artifact(tmp_path))
    await _seed_store(config, vectors=1, **_E5_STAMP)
    real_read = SqliteBackend.read_embedding_stamp_fresh

    async def _read_missing_the_vector(self):
        return {**(await real_read(self)), "vectors": 0}

    monkeypatch.setattr(SqliteBackend, "read_embedding_stamp_fresh", _read_missing_the_vector)

    await _assert_revert_refused(config, "another process changed the stored embedding")


def _revert_visible_state(app: AppContext) -> dict[str, object]:
    """Everything a failed revert must leave as it found it (#2428)."""
    comp = app._components
    assert comp is not None
    storage = app.storage
    return {
        "embedder": comp.embedder,
        "generation": comp.generation,
        "search_pipeline": comp.search_pipeline,
        "index_engine": comp.index_engine,
        "dedup_scanner": app.dedup_scanner,
        "retired_generations": list(comp.retired_generations),
        "embedding_object": app.config.embedding,
        "embedding_values": app.config.embedding.model_dump(),
        "policy": (
            storage._embedding_policy_fingerprint,
            storage._embedding_max_sequence_tokens,
        ),
        "mismatch": storage.embedding_mismatch,
    }


def _assert_revert_state_unchanged(app: AppContext, before: dict[str, object]) -> None:
    after = _revert_visible_state(app)
    for key in ("embedder", "generation", "search_pipeline", "index_engine", "dedup_scanner"):
        assert after[key] is before[key], f"{key} was replaced by a failed revert"
    assert after["embedding_object"] is before["embedding_object"]
    for key in ("retired_generations", "embedding_values", "policy", "mismatch"):
        assert after[key] == before[key], f"{key} changed after a failed revert"


async def test_an_unvalidated_namespace_glob_leaves_the_runtime_untouched(
    degraded_components,
):
    """#2428: even an unvalidated rule must not cause a partial revert.

    #2432 rejects invalid globs at the config boundary. Bypass validation
    deliberately to keep exercising a real engine-construction failure
    through the public recovery tool.
    """
    from memtomem.config import NamespacePolicyRule

    app = _make_app(degraded_components)
    ctx = _StubCtx(app)
    watcher = MagicMock(name="watcher")
    app._watcher = watcher
    assert app.dedup_scanner is not None, "fixture must make the dedup rebind observable"

    app.config.namespace.rules = [
        NamespacePolicyRule.model_construct(path_glob="[z-a]", namespace="probe")
    ]
    before = _revert_visible_state(app)
    assert before["mismatch"] is not None

    out = await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

    # The exception class name differs across Python versions; the contract is
    # that the call reports a failure instead of a completed revert.
    assert out.startswith("Error"), out
    assert "Reverted" not in out
    _assert_revert_state_unchanged(app, before)
    watcher.rebind.assert_not_called()


@pytest.mark.parametrize("failing", ["SearchPipeline", "IndexEngine", "DedupScanner"])
async def test_every_generation_constructor_fails_before_anything_is_published(
    degraded_components, failing
):
    """#2428: each constructor of the new generation runs before the first
    publication. Injected at every boundary, including the last one (the
    dedup scanner), so moving any of them back behind a publication fails."""
    from memtomem.embedding.factory import create_embedder
    from memtomem.indexing.engine import IndexEngine
    from memtomem.search.dedup import DedupScanner
    from memtomem.runtime.components import create_search_pipeline
    from memtomem.server.tools.status_config import _revert_to_stored_locked

    app = _make_app(degraded_components)
    watcher = MagicMock(name="watcher")
    app._watcher = watcher
    assert app.dedup_scanner is not None, "fixture must reach the dedup constructor"

    constructors: dict[str, object] = {
        "SearchPipeline": create_search_pipeline,
        "IndexEngine": IndexEngine,
        "DedupScanner": DedupScanner,
    }
    calls: list[str] = []

    def _raising(*_args: object, **_kwargs: object) -> object:
        calls.append(failing)
        raise RuntimeError(f"injected {failing} failure")

    constructors[failing] = _raising
    before = _revert_visible_state(app)

    with pytest.raises(RuntimeError, match=f"injected {failing} failure"):
        await _revert_to_stored_locked(
            app,
            create_embedder,
            constructors["IndexEngine"],
            constructors["DedupScanner"],
            constructors["SearchPipeline"],
        )

    # Reached the injected boundary exactly once: an earlier constructor
    # failing first would otherwise pass this test vacuously.
    assert calls == [failing]
    _assert_revert_state_unchanged(app, before)
    watcher.rebind.assert_not_called()


@pytest.mark.parametrize(
    ("provider", "model"),
    [("onnx", None), (None, "bge-m3"), ("onnx", ""), ("", "bge-m3")],
)
async def test_partial_identity_refuses_revert_before_factory(
    tmp_path, monkeypatch, provider, model
):
    config = _degraded_config(tmp_path, monkeypatch)
    with sqlite3.connect(config.storage.sqlite_path) as db:
        db.execute(
            "DELETE FROM _memtomem_meta WHERE key IN ('embedding_provider', 'embedding_model')"
        )
        for key, value in (("embedding_provider", provider), ("embedding_model", model)):
            if value is not None:
                db.execute("INSERT INTO _memtomem_meta VALUES (?, ?)", (key, value))
    comp = await create_components(config)
    try:
        app = _make_app(comp)
        ctx = _StubCtx(app)
        storage = app.storage
        mismatch = storage.embedding_mismatch
        assert mismatch is not None and mismatch["model_mismatch"]
        assert comp.embedding_broken == mismatch
        embedding = config.embedding
        before = embedding.model_dump()
        published = (comp.embedder, comp.search_pipeline, comp.index_engine, comp.generation)
        policy = (storage._embedding_policy_fingerprint, storage._embedding_max_sequence_tokens)
        with sqlite3.connect(config.storage.sqlite_path) as db:
            meta_before = db.execute("SELECT * FROM _memtomem_meta ORDER BY key").fetchall()
        factory = MagicMock(side_effect=AssertionError("partial stamp reached factory"))
        monkeypatch.setattr("memtomem.embedding.factory.create_embedder", factory)
        recover = AsyncMock()
        monkeypatch.setattr(app, "recover_from_degraded", recover)

        status = await mem_embedding_reset(ctx=ctx)
        assert "unknown" in status and "unavailable" in status
        for _ in range(2):
            result = await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)
            assert result.startswith("Error: Cannot revert")
            assert "unknown" in result and "apply-current" in result
        factory.assert_not_called()
        recover.assert_not_called()
        assert config.embedding is embedding and embedding.model_dump() == before
        assert (
            comp.embedder,
            comp.search_pipeline,
            comp.index_engine,
            comp.generation,
        ) == published
        assert (
            storage._embedding_policy_fingerprint,
            storage._embedding_max_sequence_tokens,
        ) == policy
        assert storage.embedding_mismatch == mismatch
        with sqlite3.connect(config.storage.sqlite_path) as db:
            assert db.execute("SELECT * FROM _memtomem_meta ORDER BY key").fetchall() == meta_before

        # Existing write guards and BM25 recovery remain available for partial stamps.
        from memtomem.server.helpers import _check_embedding_mismatch

        assert "indexing blocked" in _check_embedding_mismatch(app)
        await comp.search_pipeline.search("no data yet", top_k=1)
        result = await mem_embedding_reset(mode="apply_current", ctx=ctx)
        assert "DB reset" in result
        assert storage.embedding_mismatch is None
        recover.assert_awaited_once()
    finally:
        await close_components(comp)


async def test_revert_to_stored_rebinds_watcher_and_dedup(degraded_components):
    """The watcher captured the old engine/pipeline at init; without a rebind,
    post-revert auto-reindexes run through the retired engine and its retired
    embedder while cache invalidation hits a pipeline nobody queries (the
    #2141 contract, inverted). The dedup scanner is rebuilt too, but it holds
    only storage now that its scan searches with stored vectors."""
    from unittest.mock import MagicMock

    app = _make_app(degraded_components)
    ctx = _StubCtx(app)
    watcher = MagicMock(name="watcher")
    app._watcher = watcher
    pre_dedup = app.dedup_scanner
    assert pre_dedup is not None

    await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

    watcher.rebind.assert_called_once_with(app.index_engine, app.search_pipeline)
    assert app.dedup_scanner is not pre_dedup
    assert app.dedup_scanner._storage is app.storage
    assert vars(app.dedup_scanner) == {"_storage": app.storage}


# ── #2181: the reset brings the suppressed background loops back ──────


async def test_apply_current_starts_the_suppressed_watcher(degraded_app):
    """Degraded startup leaves the watcher constructed but stopped. Before
    #2181 it stayed that way after a successful reset, so files dropped into
    a memory dir were not indexed until the server restarted."""
    ctx = _StubCtx(degraded_app)

    await mem_embedding_reset(mode="apply_current", ctx=ctx)  # type: ignore[arg-type]

    degraded_app._watcher.start.assert_awaited_once()
    assert degraded_app.embedding_broken is None


async def test_revert_to_stored_starts_the_suppressed_watcher(degraded_app):
    """Same recovery on the non-destructive path. The watcher must start
    *after* the rebind, or it would watch through the retired engine."""
    ctx = _StubCtx(degraded_app)

    await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

    watcher = degraded_app._watcher
    watcher.start.assert_awaited_once()
    watcher.rebind.assert_called_once_with(degraded_app.index_engine, degraded_app.search_pipeline)
    # Order matters: a start before the rebind would watch through the engine
    # this revert just retired.
    names = [call[0] for call in watcher.mock_calls]
    assert names.index("rebind") < names.index("start")
    assert degraded_app.embedding_broken is None


async def test_repeated_resets_do_not_start_duplicate_loops(degraded_app):
    """A second reset is a no-op for recovery. ``FileWatcher.start`` builds a
    fresh Observer each call, so a duplicate start leaks a thread and a
    processor task with nothing left holding the first pair."""
    degraded_app.config.health_watchdog.enabled = True
    ctx = _StubCtx(degraded_app)

    with patch("memtomem.server.health_watchdog.HealthWatchdog") as watchdog:
        watchdog.return_value.start = AsyncMock()
        watchdog.return_value.stop = AsyncMock()
        await mem_embedding_reset(mode="apply_current", ctx=ctx)  # type: ignore[arg-type]
        await mem_embedding_reset(mode="apply_current", ctx=ctx)  # type: ignore[arg-type]
        # ... and across modes: the revert path returns early on "nothing to
        # revert", which must also not re-enter recovery.
        await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

    degraded_app._watcher.start.assert_awaited_once()
    assert watchdog.call_count == 1


async def test_reset_survives_a_failing_recovery_and_retries_it(degraded_app, caplog):
    """The repair the user asked for has already landed when recovery runs, so
    a service that fails to start is logged — never raised — and the next
    reset tries it again."""
    degraded_app._watcher.start = AsyncMock(side_effect=RuntimeError("watcher boom"))
    degraded_app.config.health_watchdog.enabled = True
    ctx = _StubCtx(degraded_app)

    with patch("memtomem.server.health_watchdog.HealthWatchdog") as watchdog:
        watchdog.return_value.start = AsyncMock()
        watchdog.return_value.stop = AsyncMock()
        with caplog.at_level(logging.WARNING):
            out = await mem_embedding_reset(mode="apply_current", ctx=ctx)  # type: ignore[arg-type]

        assert "DB reset to onnx/bge-m3" in out
        assert "Failed to start the file watcher" in caplog.text
        # A failed watcher does not keep the other services down.
        assert degraded_app.health_watchdog is watchdog.return_value
        assert degraded_app._watcher_started is False

        degraded_app._watcher.start = AsyncMock()
        await mem_embedding_reset(mode="apply_current", ctx=ctx)  # type: ignore[arg-type]

    degraded_app._watcher.start.assert_awaited_once()
    assert degraded_app._watcher_started is True


async def test_a_cancelled_watcher_cleanup_bars_the_retry(degraded_app):
    """Cancellation must not carry away the fact that the instance is barred.

    The recovery start fails and the stop meant to clean up after it is
    cancelled, so the observer and the processor task that failed start left
    behind are still live. ``FileWatcher.start`` would overwrite both, and then
    nothing could ever stop them — so a later reset must refuse, and only
    shutdown may touch that instance. The flag therefore has to be settled
    before the cancellation propagates out of recovery.
    """
    degraded_app._watcher = MagicMock()
    degraded_app._watcher.start = AsyncMock(side_effect=OSError("no inotify"))
    degraded_app._watcher.stop = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await degraded_app.recover_from_degraded()

    assert degraded_app._watcher_started is False
    assert degraded_app._watcher_cleanup_failed is True

    # The barred instance is not started over on a later attempt.
    degraded_app._watcher.start = AsyncMock()
    await degraded_app.recover_from_degraded()
    degraded_app._watcher.start.assert_not_awaited()

    # Shutdown is the one thing still allowed to touch it, and the fixture's
    # teardown does exactly that — leave it a stop it can complete.
    degraded_app._watcher.stop = AsyncMock()


async def test_revert_retries_recovery_without_a_destructive_reset(degraded_app):
    """The non-destructive mode has to stay the retry path. ``revert_to_stored``
    returns early once the mismatch is gone, so without recovery on that branch
    a user whose watchdog failed to start could only retry via
    ``apply_current`` — which drops every vector to restart a scheduler."""
    degraded_app.config.health_watchdog.enabled = True
    ctx = _StubCtx(degraded_app)

    with patch("memtomem.server.health_watchdog.HealthWatchdog") as watchdog:
        watchdog.return_value.start = AsyncMock(side_effect=RuntimeError("boom"))
        watchdog.return_value.stop = AsyncMock()
        await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]
        assert degraded_app.health_watchdog is None

        watchdog.return_value.start = AsyncMock()
        out = await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

    assert "nothing to revert" in out
    assert degraded_app.health_watchdog is watchdog.return_value
    # The retry must not re-start the watcher that came up on the first pass.
    degraded_app._watcher.start.assert_awaited_once()


async def test_revert_still_retires_the_old_generation_when_recovery_is_cancelled(
    degraded_app,
):
    """Recovery runs before the retirement closes, so a cancellation raised
    inside it must be deferred — propagating it there would skip both closes
    and re-open the #2176 leak (a leaked ONNX session + its executor thread)."""
    from unittest.mock import MagicMock

    degraded_app._watcher.start = AsyncMock(side_effect=asyncio.CancelledError())
    old_pipeline = degraded_app.search_pipeline
    old_pipeline.close = AsyncMock()
    old_embedder = degraded_app.embedder
    old_embedder.close = AsyncMock()
    ctx = _StubCtx(degraded_app)

    with pytest.raises(asyncio.CancelledError):
        await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

    old_pipeline.close.assert_awaited_once()
    old_embedder.close.assert_awaited_once()
    assert isinstance(degraded_app._watcher, MagicMock)
    # The half-started watcher is stopped rather than left running with its
    # handles about to be overwritten by the next attempt.
    degraded_app._watcher.stop.assert_awaited_once()


async def test_mem_watchdog_distinguishes_suppressed_from_disabled(degraded_app):
    """A missing watchdog handle used to read as a config problem in all
    three cases, sending the user to an env var that is already set."""
    from memtomem.server.tools.watchdog import mem_watchdog

    ctx = _StubCtx(degraded_app)

    # 1. Genuinely disabled — unchanged message.
    assert "Set MEMTOMEM_HEALTH_WATCHDOG__ENABLED=true" in await mem_watchdog(ctx=ctx)  # type: ignore[arg-type]

    # 2. Enabled but suppressed by the degraded start.
    degraded_app.config.health_watchdog.enabled = True
    suppressed = await mem_watchdog(ctx=ctx)  # type: ignore[arg-type]
    assert "degraded embedding mode" in suppressed
    assert "mem_embedding_reset" in suppressed

    # 3. Recovered, but the watchdog's start failed: no longer degraded, so
    # the message must point at the log and the retry, not at the config.
    with patch("memtomem.server.health_watchdog.HealthWatchdog") as watchdog:
        watchdog.return_value.start = AsyncMock(side_effect=RuntimeError("boom"))
        watchdog.return_value.stop = AsyncMock()
        await mem_embedding_reset(mode="apply_current", ctx=ctx)  # type: ignore[arg-type]

    failed = await mem_watchdog(ctx=ctx)  # type: ignore[arg-type]
    assert "not running" in failed
    # The remediation must name a mode: bare ``mem_embedding_reset`` defaults
    # to mode="status", which prints a report and retries nothing.
    assert 'mem_embedding_reset(mode="revert_to_stored")' in failed


async def test_recovered_watcher_indexes_a_new_file_without_a_restart(tmp_path, monkeypatch):
    """Acceptance criterion 1, against a real ``FileWatcher``: after the
    reset, a file dropped into a memory dir is auto-indexed in-process."""
    from watchdog.observers.polling import PollingObserver

    from memtomem.indexing import watcher as watcher_mod

    # Poll instead of using the platform-native backend: FSEvents/inotify are
    # unavailable in some sandboxes, where a native-only test fails for
    # reasons that have nothing to do with the recovery under test.
    monkeypatch.setattr(
        watcher_mod, "Observer", lambda: PollingObserver(timeout=0.05), raising=False
    )
    real_watcher = watcher_mod.FileWatcher
    monkeypatch.setattr(
        watcher_mod,
        "FileWatcher",
        lambda *args, **kwargs: real_watcher(*args, debounce_ms=50, **kwargs),
    )

    config = _degraded_config(tmp_path, monkeypatch)
    mem_dir = Path(config.indexing.memory_dirs[0])
    app = AppContext(config=config)
    await app.ensure_initialized()
    try:
        assert app.embedding_broken is not None
        ctx = _StubCtx(app)
        await mem_embedding_reset(mode="apply_current", ctx=ctx)  # type: ignore[arg-type]

        (mem_dir / "post-recovery.md").write_text(
            "# Post recovery\n\nWatcher picked this up without a restart.\n",
            encoding="utf-8",
        )

        # Poll rather than sleep a fixed span: the watcher debounce plus the
        # index round-trip has no bound worth pinning, only a deadline.
        deadline = time.monotonic() + 30.0
        indexed = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            indexed = (await app.storage.get_stats()).get("total_chunks", 0)
            if indexed:
                break
        assert indexed, "watcher never indexed the new file after recovery"
    finally:
        await app.close()


# ── #2180: the retired generation is closed by lease, not immediately ──


def _count_closes(app, closed: list[str]):
    """Replace the live pipeline/embedder closes with order-recording stubs.

    Returns the two instances so a test can assert against identity after the
    swap has moved ``app.embedder`` / ``app.search_pipeline`` on.
    """
    embedder = app.embedder
    pipeline = app.search_pipeline

    def _record(name):
        async def _close():
            closed.append(name)

        return _close

    embedder.close = _record("embedder")
    pipeline.close = _record("pipeline")
    return embedder, pipeline


async def test_inflight_search_keeps_the_retired_generation_open(degraded_components):
    """A search that entered before the revert must finish on the generation
    it started with. Pre-#2180 the revert closed the embedder inline, so the
    dense leg could resume against a closed ONNX session (``_closing`` latched,
    inference executor shut down with ``cancel_futures=True``)."""
    app = _make_app(degraded_components)
    ctx = _StubCtx(app)
    closed: list[str] = []
    old_embedder, old_pipeline = _count_closes(app, closed)
    old_generation = degraded_components.generation

    # Stand in for a search parked mid-pipeline: the lease is what the ranked
    # search body holds across its awaits.
    with old_generation.hold():
        out = await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

        assert "Reverted to stored DB settings" in out
        assert app.embedder is not old_embedder, "the new generation must be published"
        assert closed == [], "the retired generation was closed under an in-flight lease"

    # Last release schedules the deferred close as a background task.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert closed == ["pipeline", "embedder"]
    assert app.search_pipeline is not old_pipeline


async def test_retired_generation_closes_exactly_once(degraded_components):
    """Two leaseholders, one close. The pop-before-schedule latch means the
    first release to reach zero owns the close and every later path — another
    release, the shutdown drain — finds nothing to run."""
    app = _make_app(degraded_components)
    ctx = _StubCtx(app)
    closed: list[str] = []
    _count_closes(app, closed)
    old_generation = degraded_components.generation

    with old_generation.hold():
        with old_generation.hold():
            await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]
            assert closed == []
        assert closed == [], "close fired while a second lease was still held"

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert closed == ["pipeline", "embedder"]

    # The fixture's own ``close_components`` drains retired generations too;
    # it must not close this pair a second time.
    await close_components(degraded_components)
    assert closed == ["pipeline", "embedder"]


async def test_revert_with_no_inflight_work_closes_inline(degraded_components):
    """Acceptance criterion 3: an idle revert must not defer anything — the
    close completes before the tool returns, with no background task left."""
    app = _make_app(degraded_components)
    ctx = _StubCtx(app)
    closed: list[str] = []
    _count_closes(app, closed)
    old_generation = degraded_components.generation

    await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

    assert closed == ["pipeline", "embedder"], "idle revert deferred its close"
    assert old_generation._close_cb is None
    assert old_generation._close_task is None


async def test_second_revert_leaves_the_older_leased_generation_pinned(degraded_components):
    """Generations are independent: a still-leased gen N-2 must not be closed
    by the revert that retires gen N-1, and gen N-1 (idle) closes inline."""
    app = _make_app(degraded_components)
    ctx = _StubCtx(app)
    closed: list[str] = []
    _count_closes(app, closed)
    gen1 = degraded_components.generation

    with gen1.hold():
        await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]
        assert closed == []

        # Re-arm the mismatch so a second revert has something to swap.
        # ``embedding_mismatch`` is derived from these raw tuples
        # (stored provider, stored model, configured provider, configured model).
        app.storage._model_mismatch = ("onnx", "bge-m3", "onnx", "bge-large")
        # The revert reads the stamp from the file too (#2617), so the file
        # has to record the identity the re-armed mismatch names.
        db = app.storage._get_db()
        db.executemany(
            "INSERT OR REPLACE INTO _memtomem_meta(key, value) VALUES (?, ?)",
            [("embedding_provider", "onnx"), ("embedding_model", "bge-m3")],
        )
        db.commit()
        gen2 = degraded_components.generation
        gen2_embedder = app.embedder
        gen2_pipeline = app.search_pipeline
        gen2_closed: list[str] = []
        _count_closes(app, gen2_closed)

        await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

        assert gen2_closed == ["pipeline", "embedder"], "idle gen N-1 must close inline"
        assert gen2 is not gen1
        assert app.embedder is not gen2_embedder
        assert app.search_pipeline is not gen2_pipeline
        assert closed == [], "gen N-2 closed while still leased"

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert closed == ["pipeline", "embedder"]


async def test_shutdown_drains_a_generation_nobody_released(degraded_components):
    """A leaseholder that never releases (hung or cancelled task) would pin
    the retired ONNX session for the life of the process. ``close_components``
    is the backstop, and a late release must then schedule nothing."""
    app = _make_app(degraded_components)
    ctx = _StubCtx(app)
    closed: list[str] = []
    _count_closes(app, closed)
    old_generation = degraded_components.generation

    lease = old_generation.hold()
    lease.__enter__()
    await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]
    assert closed == []
    # Pruning settled entries (#2201) must not reach this one: its close is
    # still pending, which is the whole reason the list exists.
    assert degraded_components.retired_generations == [old_generation]

    await close_components(degraded_components)
    assert closed == ["pipeline", "embedder"]

    lease.__exit__(None, None, None)
    await asyncio.sleep(0)
    assert closed == ["pipeline", "embedder"], "the late release closed it a second time"


async def test_a_real_search_survives_a_revert_end_to_end(degraded_components):
    """The acceptance criterion over the factory wiring, not a hand-held
    lease: a real ``pipeline.search()`` parked in retrieval must finish on the
    generation it started with, and only then may that generation close.

    Pinning the whole chain matters — a ``Components`` whose container,
    pipeline and engine ended up on three different handles would still pass
    the hand-held variants above while closing the embedder mid-search here.

    Retrieval, not the dense leg: this stack is degraded, and a live embedding
    mismatch suppresses dense retrieval outright (``use_dense`` in
    ``pipeline.search``), so BM25 is where a search in this state actually
    parks.
    """
    app = _make_app(degraded_components)
    ctx = _StubCtx(app)
    pipeline = app.search_pipeline
    embedder = app.embedder
    generation = degraded_components.generation
    closed: list[str] = []

    entered = asyncio.Event()
    release = asyncio.Event()
    real_bm25 = app.storage.bm25_search

    async def _blocked_bm25(*args, **kwargs):
        entered.set()
        await release.wait()
        return await real_bm25(*args, **kwargs)

    app.storage.bm25_search = _blocked_bm25

    def _record(name):
        async def _close():
            closed.append(name)

        return _close

    embedder.close = _record("embedder")
    pipeline.close = _record("pipeline")

    search_task = asyncio.create_task(pipeline.search("anything", top_k=5))
    await entered.wait()
    assert generation.leases == 1, "the real search path did not lease its generation"

    out = await mem_embedding_reset(mode="revert_to_stored", ctx=ctx)  # type: ignore[arg-type]

    assert "Reverted to stored DB settings" in out
    assert app.embedder is not embedder, "the new generation must be published"
    assert closed == [], "the revert closed the retired generation under a live search"

    release.set()
    await search_task

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert closed == ["pipeline", "embedder"]


async def test_components_aligns_the_generation_across_the_triple(degraded_components):
    """A hand-assembled ``Components`` (CLI stacks, tests,
    ``from_components`` callers) must not end up with the container counting
    one handle while the pipeline and engine count two others — a revert would
    then read zero leases and close an embedder two live components use."""
    from memtomem.runtime.components import Components

    comp = Components(
        config=degraded_components.config,
        storage=degraded_components.storage,
        embedder=degraded_components.embedder,
        index_engine=degraded_components.index_engine,
        search_pipeline=degraded_components.search_pipeline,
    )

    assert comp.generation is comp.search_pipeline._generation
    assert comp.generation is comp.index_engine._generation

    with comp.search_pipeline._generation.hold():
        assert comp.generation.leases == 1


# #2433: startup and revert must preserve the same search features and ownership.
class _RevertReranker:
    def __init__(self):
        self.calls = []
        self.close_calls = 0

    async def rerank(self, query, results, top_k):
        from dataclasses import replace

        assert self.close_calls == 0, "search reached a closed reranker"
        self.calls.append(query)
        return [
            replace(result, rank=rank, score=1.0 / rank, source="reranked")
            for rank, result in enumerate(reversed(results), 1)
        ][:top_k]

    async def close(self):
        self.close_calls += 1


async def _search_feature_components(tmp_path, monkeypatch, *, rerank=True, expansion="tags"):
    from memtomem.models import Chunk, ChunkMetadata, SearchResult

    config = _degraded_config(tmp_path, monkeypatch)
    config.search.enable_dense = False
    config.rerank.enabled = rerank
    config.query_expansion.enabled = expansion != "disabled"
    config.query_expansion.strategy = "tags" if expansion == "disabled" else expansion
    config.llm.enabled = True
    config.decay.enabled = False
    config.access.enabled = False
    rerankers = []

    def _reranker(_config):
        instance = _RevertReranker()
        rerankers.append(instance)
        return instance

    llm = MagicMock(name="shared_llm")
    llm.generate = AsyncMock(return_value="databases")
    llm.close = AsyncMock()
    monkeypatch.setattr("memtomem.search.reranker.factory.create_reranker", _reranker)
    monkeypatch.setattr("memtomem.llm.factory.create_llm", lambda _config: llm)
    comp = await create_components(config)
    results = [
        SearchResult(
            chunk=Chunk(content=name, metadata=ChunkMetadata(source_file=tmp_path / f"{name}.md")),
            score=1.0 / rank,
            rank=rank,
            source="bm25",
        )
        for rank, name in enumerate(("first", "second"), 1)
    ]
    monkeypatch.setattr(comp.storage, "bm25_search", AsyncMock(return_value=results))
    monkeypatch.setattr(comp.storage, "get_tag_counts", AsyncMock(return_value=[("databases", 1)]))
    return comp, rerankers, llm


def _assert_search_config_wiring(comp):
    pipeline = comp.search_pipeline
    for attribute, section in {
        "_config": "search",
        "_decay_config": "decay",
        "_mmr_config": "mmr",
        "_access_config": "access",
        "_rerank_config": "rerank",
        "_expansion_config": "query_expansion",
        "_importance_config": "importance",
        "_entity_boost_config": "entity_boost",
        "_context_window_config": "context_window",
        "_session_summary_config": "session_summary",
    }.items():
        assert getattr(pipeline, attribute) is getattr(comp.config, section), attribute
    assert pipeline.storage is comp.storage
    assert pipeline._embedder is comp.embedder
    assert pipeline.llm_provider is comp.llm
    assert pipeline._generation is comp.generation is comp.index_engine._generation


@pytest.mark.parametrize("rerank", [False, True])
@pytest.mark.parametrize("expansion", ["disabled", "tags", "llm"])
async def test_revert_preserves_search_features(tmp_path, monkeypatch, rerank, expansion):
    comp, rerankers, llm = await _search_feature_components(
        tmp_path, monkeypatch, rerank=rerank, expansion=expansion
    )
    app = _make_app(comp)
    try:
        for phase in ("startup", "reverted"):
            _assert_search_config_wiring(comp)
            results, stats = await app.search_pipeline.search(
                "database memory", top_k=2, record=False
            )
            assert [r.chunk.content for r in results] == (
                ["second", "first"] if rerank else ["first", "second"]
            ), phase
            assert stats.rerank_applied is rerank
            assert stats.score_scale == ("rerank" if rerank else "bm25")
            expected = "database memory" + (" databases" if expansion != "disabled" else "")
            assert comp.storage.bm25_search.await_args.args[0] == expected
            if rerank:
                assert rerankers[-1].calls == [expected]
            if phase == "startup":
                out = await mem_embedding_reset(mode="revert_to_stored", ctx=_StubCtx(app))
                assert "Reverted to stored DB settings" in out
                assert len(rerankers) == (2 if rerank else 0)
                if rerank:
                    assert rerankers[0] is not rerankers[1]
                    assert rerankers[0].close_calls == 1
                    assert rerankers[1].close_calls == 0
        assert llm.generate.await_count == (2 if expansion == "llm" else 0)
        assert comp.storage.get_tag_counts.await_count == (2 if expansion == "tags" else 0)
        llm.close.assert_not_awaited()
    finally:
        await close_components(comp)
    assert all(r.close_calls == 1 for r in rerankers)


async def test_inflight_search_retains_its_own_reranker_across_revert(tmp_path, monkeypatch):
    comp, rerankers, _llm = await _search_feature_components(tmp_path, monkeypatch)
    app = _make_app(comp)
    old_generation = comp.generation
    entered = asyncio.Event()
    release = asyncio.Event()
    retrieve = comp.storage.bm25_search

    async def _blocked_first_search(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await retrieve(*args, **kwargs)

    monkeypatch.setattr(comp.storage, "bm25_search", _blocked_first_search)
    task = asyncio.create_task(app.search_pipeline.search("database memory", record=False))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        out = await mem_embedding_reset(mode="revert_to_stored", ctx=_StubCtx(app))
        assert "Reverted to stored DB settings" in out
        assert len(rerankers) == 2
        assert [r.close_calls for r in rerankers] == [0, 0]
        results, stats = await app.search_pipeline.search("database memory", record=False)
        assert stats.rerank_applied and results[0].chunk.content == "second"
        assert len(rerankers[1].calls) == 1
        assert rerankers[0].calls == []
        release.set()
        results, stats = await task
        assert stats.rerank_applied and results[0].chunk.content == "second"
        await old_generation.drain()
        assert len(rerankers[0].calls) == 1
        assert [r.close_calls for r in rerankers] == [1, 0]
    finally:
        release.set()
        await task
        await close_components(comp)
    assert [r.close_calls for r in rerankers] == [1, 1]


@pytest.mark.parametrize("failing", ["reranker", "pipeline", "engine", "dedup"])
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("cleanup_type", [None, RuntimeError, asyncio.CancelledError])
async def test_revert_cleans_only_unpublished_search_resources(
    tmp_path, monkeypatch, failing, failure_type, cleanup_type
):
    import memtomem.runtime.components as factory
    from memtomem.embedding import factory as embedding_factory
    from memtomem.indexing import engine as engine_module
    from memtomem.search import dedup as dedup_module
    from memtomem.search.reranker import factory as reranker_factory
    from memtomem.server.tools.status_config import _revert_to_stored

    comp, rerankers, llm = await _search_feature_components(tmp_path, monkeypatch)
    app = _make_app(comp)
    watcher = MagicMock(name="watcher")
    app._watcher = watcher
    before = _revert_visible_state(app)
    closed = []
    original_error = failure_type("construction failed")

    def _fail(*_args, **_kwargs):
        raise original_error

    async def _close(label):
        _assert_revert_state_unchanged(app, before)
        closed.append(label)
        if cleanup_type is not None:
            raise cleanup_type("cleanup failed")

    new_embedder = _FakeEmbedder()
    new_embedder.close = lambda: _close("embedder")
    new_reranker = _RevertReranker()
    new_reranker.close = lambda: _close("reranker")
    monkeypatch.setattr(embedding_factory, "create_embedder", lambda _config: new_embedder)
    monkeypatch.setattr(reranker_factory, "create_reranker", lambda _config: new_reranker)
    module, name = {
        "reranker": (reranker_factory, "create_reranker"),
        "pipeline": (factory, "SearchPipeline"),
        "engine": (engine_module, "IndexEngine"),
        "dedup": (dedup_module, "DedupScanner"),
    }[failing]
    monkeypatch.setattr(module, name, _fail)
    try:
        with pytest.raises(failure_type) as caught:
            await _revert_to_stored(app)
        assert caught.value is original_error
        _assert_revert_state_unchanged(app, before)
        watcher.rebind.assert_not_called()
        assert closed == (["embedder"] if failing == "reranker" else ["reranker", "embedder"])
        assert rerankers[0].close_calls == 0
        llm.close.assert_not_awaited()
        results, stats = await app.search_pipeline.search("database memory", record=False)
        assert stats.rerank_applied and results[0].chunk.content == "second"
    finally:
        await close_components(comp)
