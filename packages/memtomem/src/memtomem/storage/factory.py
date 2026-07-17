"""Storage backend factory."""

from __future__ import annotations

from memtomem.config import Mem2MemConfig, embedding_policy_fingerprint
from memtomem.storage.sqlite_backend import SqliteBackend


def create_storage(config: Mem2MemConfig) -> SqliteBackend:
    """Return the SQLite storage backend."""
    return SqliteBackend(
        config.storage,
        dimension=config.embedding.dimension,
        embedding_provider=config.embedding.provider,
        embedding_model=config.embedding.model,
        embedding_policy_fingerprint=embedding_policy_fingerprint(config.embedding),
        embedding_max_sequence_tokens=config.embedding.max_sequence_tokens,
    )
