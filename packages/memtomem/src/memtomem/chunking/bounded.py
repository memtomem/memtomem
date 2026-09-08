"""Exact, lossless body splitting with separately budgeted retrieval descriptions.

Only enabled by an explicit local tokenizer configuration. Loading the tokenizer
never creates an embedding session or downloads a model. Parser failures affect
semantic boundaries, never the token ceiling. No content-type exclusion heuristics.
"""

from __future__ import annotations

import ast
import bisect
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memtomem.models import Chunk, ChunkMetadata, ChunkType

if TYPE_CHECKING:
    from memtomem.config import IndexingConfig


@lru_cache(maxsize=4)
def _tokenizer(path: str, mtime: int, size: int) -> Any:
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(path)
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


class TokenBudget:
    def __init__(self, config: IndexingConfig):
        path = Path(config.chunk_tokenizer_path).expanduser().resolve()
        stat = path.stat()
        self.tokenizer = _tokenizer(str(path), stat.st_mtime_ns, stat.st_size)
        self.body = config.hard_max_chunk_tokens
        self.context = config.chunk_context_tokens
        self.model = config.chunk_model_tokens
        if self.body < 1:
            raise ValueError("an exact token budget must be positive")

    def count(self, text: str, *, special: bool = False) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=special).ids)

    def prefix(self, text: str, limit: int) -> int:
        """Return a verified fitting character boundary, never decoded token IDs.

        Token counts need not be monotonic over prefixes. Binary search is used
        only to find *a* fitting prefix; maximality is not part of the contract.
        Keeping original character slices avoids tokenizer normalization loss.
        """
        if self.count(text) <= limit:
            return len(text)
        low, high = 0, len(text)
        while low + 1 < high:
            mid = (low + high) // 2
            if self.count(text[:mid]) <= limit:
                low = mid
            else:
                high = mid
        if not low:
            raise ValueError("token budget cannot hold the next Unicode character")
        return low

    def trim(self, text: str, limit: int) -> str:
        return text[: self.prefix(text, limit)] if text else ""

    def spans(self, text: str, boundaries: tuple[int, ...] = ()) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        start = 0
        while start < len(text):
            end = start + self.prefix(
                text[start : start + min(65_536, max(256, self.body * 4))], self.body
            )
            if end < len(text):
                # Syntax/statement boundaries first, then complete lines. A very
                # long line finally falls back to a verified character boundary.
                i = bisect.bisect_right(boundaries, end) - 1
                preferred = boundaries[i] if i >= 0 else 0
                if preferred <= start:
                    preferred = text.rfind("\n", start, end) + 1
                if preferred > start and self.count(text[start:preferred]) <= self.body:
                    end = preferred
            spans.append((start, end))
            start = end
        return spans

    def describe(self, chunk: Chunk, description: str) -> Chunk:
        context = self.trim(description, self.context)
        chunk.metadata = replace(chunk.metadata, retrieval_context=context)
        self.validate(chunk)
        return chunk

    def validate(self, chunk: Chunk) -> None:
        prefix = chunk.metadata.retrieval_context or " > ".join(chunk.metadata.heading_hierarchy)
        if self.count(prefix) > self.context:
            raise ValueError("chunk description exceeds exact token budget")
        if self.count(chunk.content) > self.body:
            raise ValueError("chunk body exceeds exact token budget")
        if self.count(chunk.retrieval_content, special=True) > self.model:
            raise ValueError("composed retrieval input exceeds model token budget")


def bound_chunks(
    chunks: list[Chunk], config: IndexingConfig, *, preserve_source_lines: bool = False
) -> list[Chunk]:
    budget = TokenBudget(config)
    result: list[Chunk] = []
    for chunk in chunks:
        spans = budget.spans(chunk.content)
        for index, (start, end) in enumerate(spans):
            body = chunk.content[start:end]
            line = chunk.metadata.start_line + chunk.content.count("\n", 0, start)
            # Decoded JSON strings have virtual newlines: retain their source
            # scalar span rather than pretending those lines exist in the file.
            virtual = preserve_source_lines
            meta = replace(
                chunk.metadata,
                start_line=chunk.metadata.start_line if virtual else line,
                end_line=chunk.metadata.end_line
                if virtual
                else line + body[:-1].count("\n"),
                overlap_before=max(0, min(end, chunk.metadata.overlap_before) - start),
                overlap_after=max(
                    0, end - max(start, len(chunk.content) - chunk.metadata.overlap_after)
                ),
            )
            part = chunk if len(spans) == 1 else Chunk(content=body, metadata=meta)
            part.metadata = meta
            description = "\n".join(
                filter(
                    None,
                    (
                        chunk.metadata.retrieval_context,
                        " > ".join(chunk.metadata.heading_hierarchy),
                        f"File: {chunk.metadata.source_file.name}",
                        f"Lines: {meta.start_line}-{meta.end_line}; fragment {index + 1}/{len(spans)}",
                    ),
                )
            )
            result.append(budget.describe(part, description))
    return result


@dataclass
class _Symbol:
    start: int
    end: int
    name: str
    signature: str
    doc: str = ""
    kind: str = "function"


def _python_structure(text: str, lines: list[int]) -> tuple[list[_Symbol], set[int]]:
    tree = ast.parse(text)
    symbols: list[_Symbol] = []
    statements: set[int] = set()
    source_lines = text.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            statements.add(lines[node.lineno - 1])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            start = lines[first - 1]
            end = lines[node.end_lineno] if node.end_lineno else len(text)
            signature_end = node.body[0].lineno - 1 if node.body else node.lineno
            signature = "\n".join(source_lines[node.lineno - 1 : max(node.lineno, signature_end)])
            if node.body and node.body[0].lineno == node.lineno:
                import io
                import tokenize

                depth = 0
                for token in tokenize.generate_tokens(io.StringIO(signature).readline):
                    if token.string in {"(", "[", "{"}:
                        depth += 1
                    elif token.string in {")", "]", "}"}:
                        depth -= 1
                    elif token.string == ":" and depth == 0:
                        signature = signature[: token.end[1]]
                        break
            symbols.append(
                _Symbol(
                    start,
                    end,
                    node.name,
                    signature[:4000],
                    (ast.get_docstring(node, clean=False) or "")[:4000],
                    "class" if isinstance(node, ast.ClassDef) else "function",
                )
            )
    return symbols, statements


def _javascript_structure(path: Path, text: str) -> tuple[list[_Symbol], set[int]]:
    from tree_sitter import Language, Parser

    if path.suffix in {".ts", ".tsx"}:
        import tree_sitter_typescript as grammar

        language = (
            grammar.language_tsx() if path.suffix == ".tsx" else grammar.language_typescript()
        )
    else:
        import tree_sitter_javascript as js_grammar

        language = js_grammar.language()
    raw = text.encode("utf-8")
    tree = Parser(Language(language)).parse(raw)
    if tree.root_node.has_error:
        raise ValueError("invalid JavaScript/TypeScript syntax")
    # Keep offsets only for syntax boundaries, not a Python dict entry per character.
    needed: set[int] = {0}
    nodes = [tree.root_node]
    while nodes:
        node = nodes.pop()
        needed.update((node.start_byte, node.end_byte))
        nodes.extend(node.named_children)
    offsets = {0: 0}
    byte = 0
    for index, char in enumerate(text):
        byte += len(char.encode("utf-8"))
        if byte in needed:
            offsets[byte] = index + 1
    symbols: list[_Symbol] = []
    statements: set[int] = set()
    stack = [tree.root_node]
    kinds = {
        "function_declaration",
        "generator_function_declaration",
        "class_declaration",
        "method_definition",
        "arrow_function",
        "function_expression",
    }
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        start, end = offsets[node.start_byte], offsets[node.end_byte]
        if node.type.endswith(("statement", "declaration")):
            statements.add(start)
        if node.type in kinds:
            name_node = node.child_by_field_name("name")
            if name_node is None and node.parent is not None:
                name_node = node.parent.child_by_field_name("name")
            name = (
                raw[name_node.start_byte : name_node.end_byte].decode() if name_node else node.type
            )
            body = node.child_by_field_name("body")
            signature_end = offsets[body.start_byte] if body else min(end, start + 1000)
            symbols.append(
                _Symbol(
                    start,
                    end,
                    name,
                    text[start:signature_end][:4000],
                    kind="class" if "class" in node.type else "function",
                )
            )
    return symbols, statements


def chunk_code(path: Path, text: str, config: IndexingConfig) -> list[Chunk]:
    if not text.strip():
        return []
    budget = TokenBudget(config)
    lines = [0]
    lines.extend(i + 1 for i, char in enumerate(text) if char == "\n")
    if lines[-1] != len(text):
        lines.append(len(text))
    try:
        symbols, statements = (
            _python_structure(text, lines)
            if path.suffix == ".py"
            else _javascript_structure(path, text)
        )
    except (ImportError, SyntaxError, ValueError, RecursionError):
        symbols, statements = [], set()
    # Partition every source character exactly once, including decorators,
    # comments, imports and module constants that symbol-only parsers omitted.
    cuts = sorted({0, len(text), *(s.start for s in symbols), *(s.end for s in symbols)})
    result: list[Chunk] = []
    for begin, finish in zip(cuts, cuts[1:]):
        section = text[begin:finish]
        boundaries = tuple(sorted(p - begin for p in statements if begin < p < finish))
        spans = budget.spans(section, boundaries)
        enclosing = sorted(
            (s for s in symbols if s.start <= begin < s.end), key=lambda s: (s.start, -s.end)
        )
        hierarchy = (path.stem, *(s.name for s in enclosing))
        for index, (left, right) in enumerate(spans):
            start, end = begin + left, begin + right
            start_line = bisect.bisect_right(lines, start)
            end_line = bisect.bisect_right(lines, max(start, end - 1))
            kind = ChunkType.RAW_TEXT
            if enclosing:
                kind = (
                    (
                        ChunkType.PYTHON_CLASS
                        if enclosing[-1].kind == "class"
                        else ChunkType.PYTHON_FUNCTION
                    )
                    if path.suffix == ".py"
                    else ChunkType.JS_FUNCTION
                )
            chunk = Chunk(
                content=text[start:end],
                metadata=ChunkMetadata(
                    source_file=path,
                    heading_hierarchy=hierarchy,
                    chunk_type=kind,
                    start_line=start_line,
                    end_line=end_line,
                    language="python"
                    if path.suffix == ".py"
                    else ("typescript" if path.suffix in {".ts", ".tsx"} else "javascript"),
                ),
            )
            result.append(chunk)
    # Keep inter-symbol whitespace without indexing a separate blank result.
    # Only merge when the exact body budget still fits.
    packed: list[Chunk] = []
    for chunk in result:
        if (
            packed
            and not chunk.content.strip()
            and budget.count(packed[-1].content + chunk.content) <= budget.body
        ):
            previous = packed.pop()
            merged = Chunk(
                content=previous.content + chunk.content,
                metadata=replace(
                    previous.metadata,
                    end_line=chunk.metadata.end_line,
                ),
            )
            packed.append(merged)
        else:
            packed.append(chunk)
    from collections import Counter

    totals = Counter(c.metadata.heading_hierarchy for c in packed)
    seen: Counter[tuple[str, ...]] = Counter()
    cursor = 0
    for chunk in packed:
        meta = chunk.metadata
        hierarchy = meta.heading_hierarchy
        seen[hierarchy] += 1
        enclosing = sorted(
            (s for s in symbols if s.start <= cursor < s.end),
            key=lambda s: (s.start, -s.end),
        )
        nearby = text[max(0, cursor - 2000) : cursor].splitlines()[-6:]
        comments = "\n".join(
            line.strip() for line in nearby if line.lstrip().startswith(("#", "//", "/*", "*"))
        )
        identity = (
            " > ".join(hierarchy)
            + f"\nFile: {path.name}\nLines: {meta.start_line}-{meta.end_line}; "
            + f"fragment {seen[hierarchy]}/{totals[hierarchy]}"
        )
        # Identity first; reserve room for local comments and concise symbol docs.
        detail_limit = max(1, budget.context // 4)
        details = [budget.trim(comments, detail_limit)]
        for symbol in reversed(enclosing):
            details.extend(
                (budget.trim(symbol.signature, detail_limit), budget.trim(symbol.doc, detail_limit))
            )
        budget.describe(chunk, "\n".join(filter(None, [identity, *details])))
        cursor += len(chunk.content)
    return packed


def chunk_json(path: Path, text: str, config: IndexingConfig) -> list[Chunk]:
    """Split oversized containers by JSON pointer, decoding long string values.

    Source ranges always refer to the original serialized scalar/container.
    Keys, escape sequences and virtual newlines cannot invent source lines.
    Invalid JSON falls back to lossless raw text splitting.
    """
    import json

    budget = TokenBudget(config)

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key requires lossless fallback")
            result[key] = value
        return result

    decoder = json.JSONDecoder(object_pairs_hook=unique_pairs)
    chunks: list[Chunk] = []

    def whitespace(pos: int) -> int:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        return pos

    def visit(pos: int, pointer: str, depth: int = 0) -> int:
        if depth > 32:
            raise ValueError("deep JSON requires lossless fallback")
        pos = whitespace(pos)
        value, end = decoder.raw_decode(text, pos)
        raw = text[pos:end]
        if budget.count(raw) > budget.body and isinstance(value, (dict, list)) and value:
            cursor = pos + 1
            for index in range(len(value)):
                cursor = whitespace(cursor)
                if isinstance(value, dict):
                    key, cursor = decoder.raw_decode(text, cursor)
                    cursor = whitespace(cursor)
                    if text[cursor] != ":":
                        raise ValueError("invalid JSON member")
                    cursor += 1
                    segment = str(key).replace("~", "~0").replace("/", "~1")
                else:
                    segment = str(index)
                cursor = whitespace(visit(cursor, pointer + "/" + segment, depth + 1))
                if index + 1 < len(value):
                    if text[cursor] != ",":
                        raise ValueError("invalid JSON separator")
                    cursor += 1
        else:
            body = value if isinstance(value, str) and budget.count(raw) > budget.body else raw
            chunks.append(
                Chunk(
                    content=body,
                    metadata=ChunkMetadata(
                        source_file=path,
                        heading_hierarchy=(path.stem, pointer or "/"),
                        start_line=text.count("\n", 0, pos) + 1,
                        end_line=text.count("\n", 0, end) + 1,
                        retrieval_context=f"JSON pointer: {pointer or '/'}",
                    ),
                )
            )
        return end

    serialized = True
    try:
        end = visit(0, "")
        if text[end:].strip():
            raise ValueError("trailing JSON data")
    except (ValueError, RecursionError):
        serialized = False
        chunks = [
            Chunk(
                content=text,
                metadata=ChunkMetadata(
                    source_file=path,
                    start_line=1,
                    end_line=len(text.splitlines()),
                ),
            )
        ]
    return bound_chunks(chunks, config, preserve_source_lines=serialized)


def validate_budget_configuration(config: Any, previous: Any = None) -> None:
    """Validate before opening storage/models or publishing live configuration."""
    from memtomem.config import IndexingConfig

    if not isinstance(getattr(config, "indexing", None), IndexingConfig):
        return  # Compatibility with callers supplying only unrelated runtime knobs.
    if previous is not None and not isinstance(getattr(previous, "indexing", None), IndexingConfig):
        previous = None
    fields = (
        "hard_max_chunk_tokens",
        "chunk_tokenizer_path",
        "chunk_context_tokens",
        "chunk_model_tokens",
        "enrich_chunk_context",
    )
    if previous is not None and any(
        getattr(previous.indexing, field) != getattr(config.indexing, field) for field in fields
    ):
        raise ValueError("Chunk budget changes require a Core restart")
    if not config.indexing.hard_max_chunk_tokens:
        return
    TokenBudget(config.indexing)  # fail before any component/state mutation
    if (
        config.embedding.provider == "onnx"
        and config.embedding.max_sequence_tokens > 0
        and config.embedding.max_sequence_tokens < config.indexing.chunk_model_tokens
    ):
        raise ValueError("chunk_model_tokens exceeds ONNX max_sequence_tokens")
