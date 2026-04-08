"""Chunker registry: routes files to the appropriate chunker by extension."""

from __future__ import annotations

from pathlib import Path

from memtomem.models import Chunk


class ChunkerRegistry:
    """Maps file extensions to chunkers and dispatches chunk_file calls."""

    def __init__(self, chunkers: list[object]) -> None:
        self._map: dict[str, object] = {}
        for chunker in chunkers:
            for ext in chunker.supported_extensions():  # type: ignore[union-attr]
                self._map[ext] = chunker

    def get(self, extension: str) -> object | None:
        return self._map.get(extension)

    def supported_extensions(self) -> frozenset[str]:
        return frozenset(self._map)

    def chunk_file(self, file_path: Path, content: str) -> list[Chunk]:
        chunker = self._map.get(file_path.suffix)
        if chunker is None:
            return []
        return chunker.chunk_file(file_path, content)  # type: ignore[union-attr]
