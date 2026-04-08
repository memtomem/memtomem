"""Python code chunker using tree-sitter AST (optional dependency)."""

from __future__ import annotations

import logging
from pathlib import Path

from memtomem.models import Chunk, ChunkMetadata, ChunkType

logger = logging.getLogger(__name__)


class PythonChunker:
    """Chunks Python files by top-level definitions.

    Falls back to a single whole-file chunk if tree-sitter-python is not
    installed or parsing fails.
    """

    def supported_extensions(self) -> frozenset[str]:
        return frozenset({".py"})

    def chunk_file(self, file_path: Path, content: str) -> list[Chunk]:
        if not content.strip():
            return []
        try:
            return self._ast_chunk(file_path, content)
        except Exception:
            logger.debug(
                "Python AST parsing failed for %s, using fallback", file_path, exc_info=True
            )
            return self._fallback(file_path, content)

    def _ast_chunk(self, file_path: Path, content: str) -> list[Chunk]:
        import tree_sitter_python as tspython  # type: ignore[import]
        from tree_sitter import Language, Parser  # type: ignore[import]

        lang = Language(tspython.language())
        parser = Parser(lang)
        tree = parser.parse(content.encode())

        lines = content.splitlines()
        module_stem = file_path.stem
        chunks: list[Chunk] = []

        # Module docstring
        first = next(
            (n for n in tree.root_node.children if n.type != "comment"),
            None,
        )
        if first and first.type == "expression_statement":
            for child in first.children:
                if child.type == "string":
                    doc = "\n".join(lines[: first.end_point[0] + 1])
                    chunks.append(
                        Chunk(
                            content=doc,
                            metadata=ChunkMetadata(
                                source_file=file_path,
                                heading_hierarchy=(module_stem, "__module_doc__"),
                                chunk_type=ChunkType.RAW_TEXT,
                                start_line=1,
                                end_line=first.end_point[0] + 1,
                                language="python",
                            ),
                        )
                    )
                    break

        # Top-level function / class definitions
        for node in tree.root_node.children:
            actual = node
            if node.type == "decorated_definition":
                for child in node.children:
                    if child.type in ("function_definition", "class_definition"):
                        actual = child
                        break

            if actual.type not in ("function_definition", "class_definition"):
                continue

            name = self._node_name(actual, content)
            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1
            body = "\n".join(lines[start_line - 1 : end_line])

            ctype = (
                ChunkType.PYTHON_CLASS
                if actual.type == "class_definition"
                else ChunkType.PYTHON_FUNCTION
            )
            chunks.append(
                Chunk(
                    content=body,
                    metadata=ChunkMetadata(
                        source_file=file_path,
                        heading_hierarchy=(module_stem, name) if name else (module_stem,),
                        chunk_type=ctype,
                        start_line=start_line,
                        end_line=end_line,
                        language="python",
                    ),
                )
            )

        return chunks if chunks else self._fallback(file_path, content)

    @staticmethod
    def _node_name(node, content: str) -> str:
        for child in node.children:
            if child.type == "identifier":
                return content[child.start_byte : child.end_byte]
        return ""

    def _fallback(self, file_path: Path, content: str) -> list[Chunk]:
        lines = content.splitlines()
        return [
            Chunk(
                content=content,
                metadata=ChunkMetadata(
                    source_file=file_path,
                    heading_hierarchy=(file_path.stem,),
                    chunk_type=ChunkType.RAW_TEXT,
                    start_line=1,
                    end_line=len(lines),
                    language="python",
                ),
            )
        ]
