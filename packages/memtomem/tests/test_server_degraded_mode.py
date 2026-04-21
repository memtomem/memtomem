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
    return AppContext(
        config=components.config,
        storage=components.storage,
        embedder=components.embedder,
        index_engine=components.index_engine,
        search_pipeline=components.search_pipeline,
        watcher=None,  # type: ignore[arg-type]
        embedding_broken=components.embedding_broken,
    )


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
