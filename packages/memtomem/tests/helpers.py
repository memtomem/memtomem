"""Shared test helpers for memtomem tests."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from memtomem.models import Chunk, ChunkMetadata


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
