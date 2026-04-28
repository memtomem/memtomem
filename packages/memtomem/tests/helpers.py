"""Shared test helpers for memtomem tests."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from memtomem.models import Chunk, ChunkMetadata
from memtomem.server.context import AppContext

# Developer ``MEMTOMEM_*`` env vars that would override an in-test config
# and break hermeticity. Add new top-level config sections here when they
# grow an env-var binding.
_MEMTOMEM_ENV_VARS = (
    "MEMTOMEM_EMBEDDING__PROVIDER",
    "MEMTOMEM_EMBEDDING__MODEL",
    "MEMTOMEM_EMBEDDING__DIMENSION",
    "MEMTOMEM_STORAGE__SQLITE_PATH",
    "MEMTOMEM_INDEXING__MEMORY_DIRS",
    "MEMTOMEM_SCHEDULER__ENABLED",
)


def isolate_memtomem_env(monkeypatch) -> None:
    """Strip ``MEMTOMEM_*`` env vars and stub out ``load_config_overrides``
    so a freshly constructed ``Mem2MemConfig`` is not mutated by the
    developer's ``~/.memtomem/config.json`` or shell environment.

    Used directly by tests that construct their own components (e.g. the
    LangGraph adapter cases). The ``bm25_only_components`` fixture in
    ``conftest.py`` calls this internally for fixture-based callers.
    """
    for var in _MEMTOMEM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    import memtomem.config as _cfg

    monkeypatch.setattr(_cfg, "load_config_overrides", lambda c: None)


class StubCtx:
    """Minimal stand-in for MCP ``Context`` so MCP tools can be invoked
    directly from tests without a live FastMCP session.
    """

    def __init__(self, app: AppContext) -> None:
        class _RC:
            pass

        self.request_context = _RC()
        self.request_context.lifespan_context = app


def make_chunk(
    content: str = "test content",
    tags: tuple[str, ...] = (),
    namespace: str = "default",
    source: str = "test.md",
    heading: tuple[str, ...] = (),
    embedding: list[float] | None = None,
) -> Chunk:
    """Create a test Chunk with sensible defaults."""
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(f"/tmp/{source}"),
            tags=tuple(tags),
            namespace=namespace,
            heading_hierarchy=tuple(heading),
        ),
        content_hash=f"hash-{uuid4().hex[:8]}",
        embedding=embedding if embedding is not None else [0.1] * 1024,
    )
