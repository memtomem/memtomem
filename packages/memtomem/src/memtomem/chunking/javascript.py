"""JavaScript/TypeScript chunker using tree-sitter AST (optional dependency)."""

from __future__ import annotations

import logging
from pathlib import Path

from memtomem.chunking.base import split_source_lines
from memtomem.models import Chunk, ChunkMetadata, ChunkType

logger = logging.getLogger(__name__)

# Declarations only the TypeScript grammar produces.
_TS_DECLARATION_TYPES = frozenset(
    {
        "abstract_class_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
    }
)

_TOP_LEVEL_TYPES = (
    frozenset(
        {
            "function_declaration",
            "class_declaration",
            "generator_function_declaration",
            "export_statement",
            "lexical_declaration",  # const foo = () => {}
            "variable_declaration",  # var/let foo = function() {}
            "ambient_declaration",  # declare class/function/module/namespace …
            "module",  # module M {}
        }
    )
    | _TS_DECLARATION_TYPES
)

# What export_statement and ambient_declaration wrap.
_NESTED_DECLARATION_TYPES = (_TOP_LEVEL_TYPES - {"export_statement"}) | {
    "internal_module",
    "function_signature",
}

# Namespaces and modules can be named A.B or "x"; read the name field whatever its node type.
_NAMED_BY_FIELD_TYPES = frozenset({"internal_module", "module"})

# The TypeScript grammar names classes, interfaces and type aliases with type_identifier.
_NAME_TYPES = frozenset({"identifier", "type_identifier"})


def _is_namespace_statement(node) -> bool:
    # A top-level `namespace N {}` parses as an expression_statement; other expression
    # statements (`main();`) stay out of the chunks.
    return node.type == "expression_statement" and any(
        child.type == "internal_module" for child in node.children
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
        source = content.encode()
        tree = parser.parse(source)

        lines = split_source_lines(content)
        module_stem = file_path.stem
        chunks: list[Chunk] = []

        for node in tree.root_node.children:
            if node.type not in _TOP_LEVEL_TYPES and not _is_namespace_statement(node):
                continue

            name = self._extract_name(node, source)
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
    def _extract_name(cls, node, source: bytes) -> str:
        # Tree-sitter offsets are UTF-8 byte offsets into the parsed bytes, not str indices.
        if node.type in _NAMED_BY_FIELD_TYPES:
            name = node.child_by_field_name("name")
            return source[name.start_byte : name.end_byte].decode() if name else ""
        for child in node.children:
            if child.type in _NESTED_DECLARATION_TYPES:
                return cls._extract_name(child, source)
            if child.type in _NAME_TYPES:
                return source[child.start_byte : child.end_byte].decode()
            if child.type == "variable_declarator":
                # Only the binding is a name: a destructuring pattern ({x} = obj) has none,
                # and the initializer's identifier (obj) must not stand in for one.
                name = child.child_by_field_name("name")
                if name is not None and name.type == "identifier":
                    return source[name.start_byte : name.end_byte].decode()
        return ""

    def _fallback(self, file_path: Path, content: str) -> list[Chunk]:
        lang = "typescript" if file_path.suffix in {".ts", ".tsx"} else "javascript"
        lines = split_source_lines(content)
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
