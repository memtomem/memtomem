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
from unittest.mock import AsyncMock, patch

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


async def test_revert_to_stored_rebinds_watcher_and_dedup(degraded_components):
    """The watcher and dedup scanner captured the old engine/embedder at
    init; without a rebind, post-revert auto-reindexes run through the
    retired engine and its retired embedder while cache invalidation hits
    a pipeline nobody queries (the #2141 contract, inverted)."""
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
    assert app.dedup_scanner._embedder is app.embedder
    # ...and on the generation that was published with it (#2199): a scanner
    # rebuilt onto the retired handle would count its scans into a generation
    # the *next* revert no longer owns.
    assert app.dedup_scanner._generation is app._components.generation


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
