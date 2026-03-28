"""Chunker protocol."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from memtomem.models import Chunk


class Chunker(Protocol):
    def supported_extensions(self) -> frozenset[str]: ...
    def chunk_file(self, file_path: Path, content: str) -> list[Chunk]: ...
