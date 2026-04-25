"""``mem_batch_add`` per-entry tag isolation.

Pre-PR-A the chunker did not promote per-entry ``> tags:`` blockquote
headers to ``ChunkMetadata.tags``, so ``mem_batch_add`` worked around
that by post-indexing every chunk in the file with the union of every
entry's tags. Two bugs:

1. **Cross-entry leak inside the batch** — entry A's tags landed on
   entry B's chunk.
2. **Cross-batch leak onto pre-existing chunks** —
   ``list_chunks_by_source(target)`` returns *all* chunks in the file,
   not just the newly-added ones, so a fresh batch retagged unrelated
   memories that lived in the same file from prior sessions.

PR-A made the chunker promote per-entry blockquote tags directly, so
the broadcast became redundant and over-applying. This file pins the
post-removal behavior.
"""

from __future__ import annotations

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.server.component_factory import close_components, create_components
from memtomem.server.context import AppContext
from memtomem.server.tools.memory_crud import mem_batch_add


_MEMTOMEM_ENV_VARS = (
    "MEMTOMEM_EMBEDDING__PROVIDER",
    "MEMTOMEM_EMBEDDING__MODEL",
    "MEMTOMEM_EMBEDDING__DIMENSION",
    "MEMTOMEM_STORAGE__SQLITE_PATH",
    "MEMTOMEM_INDEXING__MEMORY_DIRS",
)


def _isolate_memtomem_env(monkeypatch) -> None:
    """Strip ``MEMTOMEM_*`` env vars and stub ``load_config_overrides`` so a
    freshly constructed ``Mem2MemConfig`` is not contaminated by the
    developer's ``~/.memtomem/config.json`` or shell environment.
    """
    for var in _MEMTOMEM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    import memtomem.config as _cfg

    monkeypatch.setattr(_cfg, "load_config_overrides", lambda c: None)


class _StubCtx:
    """Minimal stand-in for MCP ``Context``."""

    def __init__(self, app: AppContext) -> None:
        class _RC:
            pass

        self.request_context = _RC()
        self.request_context.lifespan_context = app


@pytest.fixture
async def integration_components(tmp_path, monkeypatch):
    """Real BM25-only stack with a tmp DB + memory_dir. Hermetic."""
    db_path = tmp_path / "batch.db"
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()

    _isolate_memtomem_env(monkeypatch)

    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]
    config.embedding.dimension = 1024
    config.search.enable_dense = False  # BM25-only — no embedder needed

    comp = await create_components(config)
    try:
        yield comp, mem_dir
    finally:
        await close_components(comp)


class TestBatchAddTagIsolation:
    @pytest.mark.asyncio
    async def test_per_entry_tags_do_not_leak_across_entries(self, integration_components):
        """Tagged entry's tags stay on its chunk; untagged entry stays untagged."""
        comp, mem_dir = integration_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        target = mem_dir / "isolation.md"
        await mem_batch_add(  # type: ignore[arg-type]
            entries=[
                {"key": "Tagged entry", "value": "Use redis for cache.", "tags": ["cache"]},
                {"key": "Untagged entry", "value": "Postgres handles persistence."},
            ],
            file=str(target),
            ctx=ctx,
        )

        chunks = await comp.storage.list_chunks_by_source(target)
        assert len(chunks) == 2

        tagged = next(c for c in chunks if "Tagged entry" in c.metadata.heading_hierarchy[0])
        untagged = next(c for c in chunks if "Untagged entry" in c.metadata.heading_hierarchy[0])

        # Tagged entry's chunk gets exactly its declared tag.
        assert set(tagged.metadata.tags) == {"cache"}
        # Untagged entry's chunk has empty tags — pre-fix this would have
        # been ``("cache",)`` because of the global union broadcast.
        assert untagged.metadata.tags == ()

    @pytest.mark.asyncio
    async def test_batch_does_not_retag_preexisting_chunks(self, integration_components):
        """A pre-existing chunk in the same file is not retagged by a new batch."""
        comp, mem_dir = integration_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        target = mem_dir / "shared.md"
        # Round 1 — seed an untagged memory.
        await mem_batch_add(  # type: ignore[arg-type]
            entries=[{"key": "Original entry", "value": "Pre-existing content."}],
            file=str(target),
            ctx=ctx,
        )
        # Round 2 — append a tagged batch to the same file.
        await mem_batch_add(  # type: ignore[arg-type]
            entries=[{"key": "New entry", "value": "Fresh content.", "tags": ["fresh"]}],
            file=str(target),
            ctx=ctx,
        )

        chunks = await comp.storage.list_chunks_by_source(target)
        assert len(chunks) == 2

        original = next(c for c in chunks if "Original entry" in c.metadata.heading_hierarchy[0])
        new = next(c for c in chunks if "New entry" in c.metadata.heading_hierarchy[0])

        # Original chunk untouched — pre-fix this would have gained
        # ``("fresh",)`` because the broadcast hit every chunk in the file.
        assert original.metadata.tags == ()
        assert set(new.metadata.tags) == {"fresh"}

    @pytest.mark.asyncio
    async def test_distinct_per_entry_tags_stay_distinct(self, integration_components):
        """Two entries with different tag sets keep them distinct (no union)."""
        comp, mem_dir = integration_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        target = mem_dir / "distinct.md"
        await mem_batch_add(  # type: ignore[arg-type]
            entries=[
                {"key": "Cache", "value": "Redis pool size 20.", "tags": ["cache"]},
                {"key": "Auth", "value": "OAuth via Keycloak.", "tags": ["auth"]},
            ],
            file=str(target),
            ctx=ctx,
        )

        chunks = await comp.storage.list_chunks_by_source(target)
        cache = next(c for c in chunks if "Cache" in c.metadata.heading_hierarchy[0])
        auth = next(c for c in chunks if "Auth" in c.metadata.heading_hierarchy[0])

        # Pre-fix both would have been ``("auth", "cache")`` after the
        # global-union broadcast.
        assert set(cache.metadata.tags) == {"cache"}
        assert set(auth.metadata.tags) == {"auth"}
