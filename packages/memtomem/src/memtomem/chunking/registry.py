"""Chunker registry: routes files to the appropriate chunker by extension."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from memtomem.chunking.base import Chunker
from memtomem.chunking.javascript import JavaScriptChunker
from memtomem.chunking.markdown import MarkdownChunker
from memtomem.chunking.python_code import PythonChunker
from memtomem.chunking.restructured_text import ReStructuredTextChunker
from memtomem.chunking.structured import StructuredChunker
from memtomem.models import Chunk

if TYPE_CHECKING:
    from memtomem.config import IndexingConfig


class ChunkerRegistry:
    """Maps file extensions to chunkers and dispatches chunk_file calls."""

    def __init__(self, chunkers: list[Chunker]) -> None:
        self._map: dict[str, Chunker] = {}
        for chunker in chunkers:
            for ext in chunker.supported_extensions():
                self._map[ext] = chunker

    def get(self, extension: str) -> Chunker | None:
        return self._map.get(extension)

    def supported_extensions(self) -> frozenset[str]:
        return frozenset(self._map)

    def chunk_file(self, file_path: Path, content: str) -> list[Chunk]:
        chunker = self._map.get(file_path.suffix)
        if chunker is None:
            return []
        return chunker.chunk_file(file_path, content)


def build_chunker_registry(indexing_config: IndexingConfig) -> ChunkerRegistry:
    """The one chunker set every indexing engine uses.

    The server's startup components and an ``IndexEngine`` built without a
    registry used to assemble this list separately and drifted apart (#2622):
    the engine's copy registered the code chunkers only under a hard chunk
    budget and built ``MarkdownChunker`` without the configured sizes. That
    copy is what the engine rebuilt by revert-to-stored and ``mm memory
    doctor`` ran, so without a hard budget they skipped code files the server
    indexes. (The budget audit builds one too, but always under a hard budget.)

    The code chunkers are registered unconditionally. Their tree-sitter
    imports happen at parse time, and a file that cannot be parsed falls back
    to one whole-file chunk, so a missing ``memtomem[code]`` extra changes how
    code is split, not whether it is indexed.
    """
    return ChunkerRegistry(
        [
            MarkdownChunker(indexing_config=indexing_config),
            StructuredChunker(indexing_config=indexing_config),
            ReStructuredTextChunker(),
            PythonChunker(),
            JavaScriptChunker(),
        ]
    )
