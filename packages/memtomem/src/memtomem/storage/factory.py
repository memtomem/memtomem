"""Storage backend factory."""

from __future__ import annotations

from memtomem.config import Mem2MemConfig
from memtomem.storage.sqlite_backend import SqliteBackend


def create_storage(config: Mem2MemConfig) -> SqliteBackend:
    """Return the SQLite storage backend."""
    return SqliteBackend(
        config.storage,
        dimension=config.embedding.dimension,
        embedding_provider=config.embedding.provider,
        embedding_model=config.embedding.model,
    )
