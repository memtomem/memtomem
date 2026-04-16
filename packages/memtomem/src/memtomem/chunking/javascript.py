"""JavaScript/TypeScript chunker using tree-sitter AST (optional dependency)."""

from __future__ import annotations

import logging
from pathlib import Path

from memtomem.models import Chunk, ChunkMetadata, ChunkType

logger = logging.getLogger(__name__)

_TOP_LEVEL_TYPES = frozenset(
    {
        "function_declaration",
        "class_declaration",
        "generator_function_declaration",
        "export_statement",
        "lexical_declaration",  # const foo = () => {}
        "variable_declaration",  # var/let foo = function() {}
    }
)


class JavaScriptChunker:
    """Chunks JS/TS files by top-level declarations.

    Falls back to a single whole-file chunk if tree-sitter-javascript /
    tree-sitter-typescript is not installed or parsing fails.
    """

    def supported_extensions(self) -> frozenset[str]:
        return frozenset({".js", ".ts", ".jsx", ".tsx", ".mjs"})

    def chunk_file(self, file_path: Path, content: str) -> list[Chunk]:
        if not content.strip():
            return []
        try:
            return self._ast_chunk(file_path, content)
        except Exception:
            logger.debug(
                "JS/TS AST parsing failed for %s, using fallback", file_path, exc_info=True
            )
            return self._fallback(file_path, content)

    def _ast_chunk(self, file_path: Path, content: str) -> list[Chunk]:
        from tree_sitter import Language, Parser

        if file_path.suffix in {".ts", ".tsx"}:
            import tree_sitter_typescript as tsts

            lang = Language(tsts.language_typescript())
            lang_name = "typescript"
        else:
            import tree_sitter_javascript as tsjs

            lang = Language(tsjs.language())
            lang_name = "javascript"

        parser = Parser(lang)
        tree = parser.parse(content.encode())

        lines = content.splitlines()
        module_stem = file_path.stem
        chunks: list[Chunk] = []

        for node in tree.root_node.children:
            if node.type not in _TOP_LEVEL_TYPES:
                continue

            name = self._extract_name(node, content)
            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1
            body = "\n".join(lines[start_line - 1 : end_line])

            chunks.append(
                Chunk(
                    content=body,
                    metadata=ChunkMetadata(
                        source_file=file_path,
                        heading_hierarchy=(module_stem, name) if name else (module_stem,),
                        chunk_type=ChunkType.JS_FUNCTION,
                        start_line=start_line,
                        end_line=end_line,
                        language=lang_name,
                    ),
                )
            )

        return chunks if chunks else self._fallback(file_path, content)

    @classmethod
    def _extract_name(cls, node, content: str) -> str:
        for child in node.children:
            if child.type in ("function_declaration", "class_declaration", "lexical_declaration"):
                return cls._extract_name(child, content)
            if child.type == "identifier":
                return content[child.start_byte : child.end_byte]
            if child.type == "variable_declarator":
                for grandchild in child.children:
                    if grandchild.type == "identifier":
                        return content[grandchild.start_byte : grandchild.end_byte]
        return ""

    def _fallback(self, file_path: Path, content: str) -> list[Chunk]:
        lang = "typescript" if file_path.suffix in {".ts", ".tsx"} else "javascript"
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
                    language=lang,
                ),
            )
        ]
