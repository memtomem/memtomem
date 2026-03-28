"""Protocols for cross-package integration (structural typing)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class IndexResult:
    """Minimal result from a file indexing operation."""

    indexed_chunks: int = 0


class FileIndexer(Protocol):
    """Protocol for file indexing integration.

    memtomem's IndexEngine structurally satisfies this protocol,
    enabling auto-indexing without hard dependency.
    """

    async def index_file(
        self,
        path: Path,
        *,
        force: bool = False,
        namespace: str | None = None,
    ) -> IndexResult: ...
