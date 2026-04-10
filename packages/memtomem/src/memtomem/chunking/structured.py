"""Structured data chunker: splits JSON/YAML/TOML files by top-level keys.

Two modes:
- "original" (default): extracts original text lines per key, splits large
  sections by line count.
- "recursive": serialises values via json.dumps and recursively splits large
  values by sub-keys.
"""

from __future__ import annotations

import json
import logging
import tomllib
from pathlib import Path

from memtomem.models import Chunk, ChunkMetadata, ChunkType

logger = logging.getLogger(__name__)

_TOKEN_CHAR_RATIO = 3  # rough chars-per-token estimate


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _TOKEN_CHAR_RATIO)


class StructuredChunker:
    """Chunks structured data files (JSON, YAML, TOML) by top-level keys.

    Falls back to a single whole-file chunk if parsing fails or the top-level
    value is not a mapping (dict).
    """

    def __init__(
        self,
        *,
        mode: str = "original",
        max_chunk_tokens: int = 512,
        indexing_config: object | None = None,
    ) -> None:
        self._explicit_mode = mode
        self._explicit_max_tokens = max_chunk_tokens
        self._indexing_config = indexing_config

    @property
    def _mode(self) -> str:
        if self._indexing_config is not None:
            return getattr(self._indexing_config, "structured_chunk_mode", self._explicit_mode)
        return self._explicit_mode

    @property
    def _max_tokens(self) -> int:
        if self._indexing_config is not None:
            return getattr(self._indexing_config, "max_chunk_tokens", self._explicit_max_tokens)
        return self._explicit_max_tokens

    def supported_extensions(self) -> frozenset[str]:
        return frozenset({".json", ".yaml", ".yml", ".toml"})

    def chunk_file(self, file_path: Path, content: str) -> list[Chunk]:
        if not content.strip():
            return []
        try:
            data = self._parse(file_path.suffix, content)
        except Exception:
            logger.debug(
                "Failed to parse %s, falling back to whole-file chunk", file_path, exc_info=True
            )
            return self._fallback(file_path, content)

        if not isinstance(data, dict):
            return self._fallback(file_path, content)

        if self._mode == "recursive":
            return self._chunk_recursive(file_path, data)
        return self._chunk_original(file_path, content, data)

    # ------------------------------------------------------------------
    # Mode: original — extract original text lines
    # ------------------------------------------------------------------

    def _chunk_original(
        self,
        file_path: Path,
        content: str,
        data: dict,
    ) -> list[Chunk]:
        lines = content.splitlines(keepends=True)
        key_ranges = self._find_key_lines(file_path.suffix, content, list(data.keys()))
        filename = file_path.stem
        chunks: list[Chunk] = []

        for key in data:
            start, end = key_ranges.get(str(key), (0, 0))
            if start == 0 or end == 0:
                # Fallback: serialise value
                text = json.dumps(data[key], indent=2, ensure_ascii=False, default=str)
                chunks.append(
                    Chunk(
                        content=text,
                        metadata=ChunkMetadata(
                            source_file=file_path,
                            heading_hierarchy=(filename, str(key)),
                            chunk_type=ChunkType.RAW_TEXT,
                            start_line=0,
                            end_line=0,
                        ),
                    )
                )
                continue

            section_text = "".join(lines[start - 1 : end]).rstrip("\n")

            if _estimate_tokens(section_text) <= self._max_tokens:
                chunks.append(
                    Chunk(
                        content=section_text,
                        metadata=ChunkMetadata(
                            source_file=file_path,
                            heading_hierarchy=(filename, str(key)),
                            chunk_type=ChunkType.RAW_TEXT,
                            start_line=start,
                            end_line=end,
                        ),
                    )
                )
            else:
                # Split large section by line groups
                chunks.extend(self._split_lines(file_path, lines, start, end, (filename, str(key))))

        return chunks if chunks else self._fallback(file_path, content)

    def _split_lines(
        self,
        file_path: Path,
        all_lines: list[str],
        start: int,
        end: int,
        hierarchy: tuple[str, ...],
    ) -> list[Chunk]:
        """Split a range of lines into chunks of at most max_tokens."""
        max_chars = self._max_tokens * _TOKEN_CHAR_RATIO
        chunks: list[Chunk] = []
        buf: list[str] = []
        buf_start = start
        buf_chars = 0

        for i in range(start - 1, end):
            line = all_lines[i]
            if buf_chars + len(line) > max_chars and buf:
                chunks.append(
                    Chunk(
                        content="".join(buf).rstrip("\n"),
                        metadata=ChunkMetadata(
                            source_file=file_path,
                            heading_hierarchy=hierarchy,
                            chunk_type=ChunkType.RAW_TEXT,
                            start_line=buf_start,
                            end_line=buf_start + len(buf) - 1,
                        ),
                    )
                )
                buf = []
                buf_start = i + 1
                buf_chars = 0
            buf.append(line)
            buf_chars += len(line)

        if buf:
            chunks.append(
                Chunk(
                    content="".join(buf).rstrip("\n"),
                    metadata=ChunkMetadata(
                        source_file=file_path,
                        heading_hierarchy=hierarchy,
                        chunk_type=ChunkType.RAW_TEXT,
                        start_line=buf_start,
                        end_line=buf_start + len(buf) - 1,
                    ),
                )
            )
        return chunks

    # ------------------------------------------------------------------
    # Mode: recursive — json.dumps + recursive sub-key splitting
    # ------------------------------------------------------------------

    def _chunk_recursive(self, file_path: Path, data: dict) -> list[Chunk]:
        filename = file_path.stem
        chunks: list[Chunk] = []
        self._find_key_lines(
            file_path.suffix,
            "",  # not used for line tracking in recursive mode
            [],
        )

        for key, value in data.items():
            self._recurse(
                file_path,
                value,
                hierarchy=(filename, str(key)),
                chunks=chunks,
            )

        return (
            chunks
            if chunks
            else self._fallback(
                file_path, json.dumps(data, indent=2, ensure_ascii=False, default=str)
            )
        )

    _MAX_DEPTH = 50

    def _recurse(
        self,
        file_path: Path,
        value: object,
        hierarchy: tuple[str, ...],
        chunks: list[Chunk],
        depth: int = 0,
    ) -> None:
        serialized = json.dumps(value, indent=2, ensure_ascii=False, default=str)

        if depth >= self._MAX_DEPTH or _estimate_tokens(serialized) <= self._max_tokens:
            chunks.append(
                Chunk(
                    content=serialized,
                    metadata=ChunkMetadata(
                        source_file=file_path,
                        heading_hierarchy=hierarchy,
                        chunk_type=ChunkType.RAW_TEXT,
                        start_line=0,
                        end_line=0,
                    ),
                )
            )
            return

        # Try splitting by sub-keys if value is a dict
        if isinstance(value, dict) and len(value) > 1:
            for sub_key, sub_val in value.items():
                self._recurse(
                    file_path,
                    sub_val,
                    hierarchy=(*hierarchy, str(sub_key)),
                    chunks=chunks,
                    depth=depth + 1,
                )
            return

        # Try splitting list items
        if isinstance(value, list) and len(value) > 1:
            for i, item in enumerate(value):
                self._recurse(
                    file_path,
                    item,
                    hierarchy=(*hierarchy, f"[{i}]"),
                    chunks=chunks,
                    depth=depth + 1,
                )
            return

        # Can't split further — emit as-is (oversized)
        chunks.append(
            Chunk(
                content=serialized,
                metadata=ChunkMetadata(
                    source_file=file_path,
                    heading_hierarchy=hierarchy,
                    chunk_type=ChunkType.RAW_TEXT,
                    start_line=0,
                    end_line=0,
                ),
            )
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_key_lines(
        suffix: str,
        content: str,
        keys: list[str],
    ) -> dict[str, tuple[int, int]]:
        """Estimate 1-based line numbers for each top-level key."""
        lines = content.splitlines()
        if not lines or not keys:
            return {}

        import re as _re

        result: dict[str, tuple[int, int]] = {}
        key_positions: list[tuple[int, str]] = []

        for key in keys:
            escaped = _re.escape(str(key))
            if suffix == ".json":
                pattern = _re.compile(rf'[{{\s,]?\s*"{escaped}"\s*:')
            elif suffix in (".yaml", ".yml"):
                pattern = _re.compile(rf"^{escaped}\s*:")
            elif suffix == ".toml":
                pattern = _re.compile(rf"^\[{escaped}\]|^{escaped}\s*=")
            else:
                continue

            for line_idx, line in enumerate(lines):
                if pattern.match(line):
                    key_positions.append((line_idx + 1, str(key)))
                    break
            else:
                key_positions.append((0, str(key)))

        key_positions.sort(key=lambda x: x[0])
        total_lines = len(lines)

        for i, (start, key) in enumerate(key_positions):
            if start == 0:
                result[key] = (0, 0)
                continue
            if i + 1 < len(key_positions) and key_positions[i + 1][0] > 0:
                end = key_positions[i + 1][0] - 1
            else:
                end = total_lines
            while end > start and not lines[end - 1].strip():
                end -= 1
            result[key] = (start, end)

        return result

    @staticmethod
    def _parse(suffix: str, content: str) -> object:
        if suffix == ".json":
            return json.loads(content)
        if suffix in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore[import]
            except ImportError as exc:
                raise ImportError(
                    "PyYAML is required for YAML chunking: pip install pyyaml"
                ) from exc
            return yaml.safe_load(content)
        if suffix == ".toml":
            return tomllib.loads(content)
        raise ValueError(f"Unsupported extension: {suffix}")

    @staticmethod
    def _fallback(file_path: Path, content: str) -> list[Chunk]:
        return [
            Chunk(
                content=content,
                metadata=ChunkMetadata(
                    source_file=file_path,
                    heading_hierarchy=(file_path.stem,),
                    chunk_type=ChunkType.RAW_TEXT,
                    start_line=0,
                    end_line=0,
                ),
            )
        ]
