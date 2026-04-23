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

import sqlite3
from pathlib import Path
from typing import Sequence

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

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
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


@pytest.fixture
async def degraded_components(tmp_path, monkeypatch):
    """``create_components`` against a dim=0 DB with config pointing at onnx/bge-m3.

    Would have raised ``EmbeddingDimensionMismatchError`` pre-#349; now returns
    ``Components`` with ``embedding_broken`` populated and a relaxed storage.
    """
    db_path = tmp_path / "legacy.db"
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()
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
        "memtomem.server.component_factory.create_embedder",
        lambda embedding_config: _FakeEmbedder(),
    )

    comp = await create_components(config)
    try:
        yield comp
    finally:
        await close_components(comp)


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
