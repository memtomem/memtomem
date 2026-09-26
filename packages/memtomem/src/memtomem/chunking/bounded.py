"""Exact, lossless body splitting with separately budgeted retrieval descriptions.

Enabled by an exact tokenizer configuration (automatic for the E5 profile).
The pinned E5 tokenizer can be downloaded without allocating an inference session.
Parser failures affect
semantic boundaries, never the token ceiling. No content-type exclusion heuristics.
"""

from __future__ import annotations

import ast
import hashlib
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
    try:
        from tokenizers import Tokenizer
    except ModuleNotFoundError as exc:
        if exc.name != "tokenizers":
            raise
        from memtomem.embedding._dependencies import missing_onnx_dependency

        raise missing_onnx_dependency(exc.name) from exc

    tokenizer = Tokenizer.from_file(path)
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


@lru_cache(maxsize=8)
def _tokenizer_digest(path: str, mtime: int, size: int) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tokenizer_fingerprint(path: Path) -> str:
    """Digest the tokenizer at *path*, re-reading it when its metadata moves.

    Keyed on mtime/size rather than remembered once, so a tokenizer replaced in
    place is a different identity. Callers that pin chunking decisions to this
    value depend on that: the configured *path* is restart-guarded, the bytes at
    it are not.
    """
    stat = path.stat()
    return _tokenizer_digest(str(path), stat.st_mtime_ns, stat.st_size)


class TokenBudget:
    def __init__(self, config: IndexingConfig):
        from memtomem.embedding.profiles import resolve_tokenizer

        path = resolve_tokenizer(config.chunk_tokenizer_path)
        stat = path.stat()
        self.tokenizer = _tokenizer(str(path), stat.st_mtime_ns, stat.st_size)
        self.fingerprint = _tokenizer_digest(str(path), stat.st_mtime_ns, stat.st_size)
        self.tokenizer_path = path
        self.body = config.hard_max_chunk_tokens
        self.context = config.chunk_context_tokens
        self.model = config.chunk_model_tokens
        self.input_prefix = config.chunk_input_prefix
        if self.body < 1:
            raise ValueError("an exact token budget must be positive")

    def count(self, text: str, *, special: bool = False) -> int:
        return len(
            self.tokenizer.encode(
                self.input_prefix + text if special else text, add_special_tokens=special
            ).ids
        )

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
        if context and self.count(chunk.retrieval_content, special=True) > self.model:
            # ``IndexingConfig`` can only bound ``hard_max + context`` against
            # ``chunk_model_tokens``; the composed input the embedder sees also
            # carries the tokenizer's special tokens and the ``\n\n`` separator,
            # which no configuration check can price without the tokenizer. A
            # body and a description that each fit can therefore still overflow
            # together, and raising here would fail the whole file for a
            # description that is free to be shorter. Trim it against the real
            # composed count instead. Token counts are not monotonic over
            # prefixes, so this searches for *a* fitting length, exactly as
            # ``prefix`` does, and never claims maximality.
            low, high = 0, len(context)
            while low < high:
                mid = (low + high + 1) // 2
                chunk.metadata = replace(chunk.metadata, retrieval_context=context[:mid])
                if self.count(chunk.retrieval_content, special=True) <= self.model:
                    low = mid
                else:
                    high = mid - 1
            # An empty ``retrieval_context`` makes ``Chunk.retrieval_content``
            # fall back to composing the heading hierarchy, which is a
            # different (and possibly longer) string than the one just
            # measured. Keep one character so the measured composition is the
            # one that ships.
            chunk.metadata = replace(chunk.metadata, retrieval_context=context[: max(1, low)])
        self.validate(chunk)
        return chunk

    def validate_description(self, chunk: Chunk) -> None:
        """Check the retrieval description against the context budget."""
        prefix = chunk.metadata.retrieval_context or " > ".join(chunk.metadata.heading_hierarchy)
        if self.count(prefix) > self.context:
            raise ValueError("chunk description exceeds exact token budget")

    def validate_body(self, chunk: Chunk) -> None:
        """Check the chunk body against the exact per-chunk ceiling.

        Split out for the one caller that must skip it: a store writing back a
        row whose body predates the budget. That is the only check a legacy row
        is exempt from — it is the one the row violates by construction, and
        refusing it would make the row permanently unwritable.
        """
        if self.count(chunk.content) > self.body:
            raise ValueError("chunk body exceeds exact token budget")

    def validate_composed(self, chunk: Chunk) -> None:
        """Check what the embedder is actually handed.

        Not the body's to waive. A body over the *per-chunk* ceiling can still
        compose well under the *model* ceiling, so exempting a legacy row from
        this one would let a caller push it past the model limit by growing only
        its description — the growing-description hole the body exemption is not
        supposed to open.
        """
        if self.count(chunk.retrieval_content, special=True) > self.model:
            raise ValueError("composed retrieval input exceeds model token budget")

    def validate(self, chunk: Chunk) -> None:
        self.validate_description(chunk)
        self.validate_body(chunk)
        self.validate_composed(chunk)


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
            virtual = preserve_source_lines or len(spans) == 1
            meta = replace(
                chunk.metadata,
                source_read_only=chunk.metadata.source_read_only
                or len(spans) > 1
                or preserve_source_lines,
                source_span_hash=None,
                start_line=chunk.metadata.start_line if virtual else line,
                end_line=chunk.metadata.end_line if virtual else line + body[:-1].count("\n"),
                overlap_before=max(0, min(end, chunk.metadata.overlap_before) - start),
                overlap_after=max(
                    0, end - max(start, len(chunk.content) - chunk.metadata.overlap_after)
                ),
            )
            part = chunk if len(spans) == 1 else Chunk(content=body, metadata=meta)
            part.metadata = meta
            # No line numbers here, deliberately. ``retrieval_context`` is the
            # embedded and BM25-indexed description, and the differ treats a
            # changed one as an embed-worthy change — so a single inserted line
            # near the top of a file would shift every following chunk's
            # description and re-embed (and re-enrich, since the LLM cache is
            # keyed on this string) the whole file, instead of taking the
            # hash-equal ``update_chunk_line_ranges`` path (#1788). The range
            # lives in ``start_line``/``end_line``, which that path refreshes,
            # and adds nothing to a vector. The fragment ordinal stays: it
            # moves only when the split itself moves.
            description = "\n".join(
                filter(
                    None,
                    (
                        chunk.metadata.retrieval_context,
                        " > ".join(chunk.metadata.heading_hierarchy),
                        f"File: {chunk.metadata.source_file.name}",
                        f"Fragment {index + 1}/{len(spans)}",
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
    """Symbols and statement boundaries, keyed to offsets in ``lines``.

    ``lines`` is built from ``\n`` alone while ``ast`` counts lines the way
    universal-newline decoding does, so text carrying a lone ``\r`` gives
    ``ast`` more lines than the table has. The indexer reads with universal
    newlines precisely so that cannot happen, but a direct caller holding raw
    bytes can still get here, and an ``IndexError`` from a parser detail should
    not cost the whole file. Every lookup goes through ``offset`` and clamps;
    ``chunk_code`` also catches ``IndexError`` as a second layer, because a
    clamp only helps at the sites that use it.
    """

    def offset(lineno: int) -> int:
        return lines[lineno] if 0 <= lineno < len(lines) else len(text)

    tree = ast.parse(text)
    symbols: list[_Symbol] = []
    statements: set[int] = set()
    source_lines = text.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            statements.add(offset(node.lineno - 1))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            start = offset(first - 1)
            end = offset(node.end_lineno) if node.end_lineno else len(text)
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


def _enclosing_symbols(symbols: list[_Symbol], start: int, end: int) -> tuple[_Symbol, ...]:
    """Symbols containing the *whole* range, outermost first.

    Containment, not "contains the first character": a merged span can outgrow
    the symbol it started in, and naming that symbol would label a chunk after a
    body it only partly holds.
    """
    return tuple(
        sorted(
            (s for s in symbols if s.start <= start and end <= s.end),
            key=lambda s: (s.start, -s.end),
        )
    )


def _pack_code_spans(
    text: str, spans: list[tuple[int, int]], budget: TokenBudget, target: int, ceiling: int
) -> list[tuple[int, int]]:
    """Greedily grow each span towards ``target`` without passing ``ceiling``."""
    if target <= 0 or len(spans) <= 1:
        return spans
    packed: list[tuple[int, int]] = []
    i = 0
    while i < len(spans):
        start, end = spans[i]
        count = budget.count(text[start:end])
        while count < target and i + 1 < len(spans):
            candidate = spans[i + 1][1]
            total = budget.count(text[start:candidate])
            if total > ceiling:
                break
            end, count = candidate, total
            i += 1
        packed.append((start, end))
        i += 1
    return packed


def _merge_short_code_spans(
    text: str, spans: list[tuple[int, int]], budget: TokenBudget, floor: int
) -> list[tuple[int, int]]:
    """Fold spans under ``floor`` into the smaller adjacent span that still fits.

    Only a *short* span initiates a merge. Letting a long span reach forward for
    a short neighbour strands the span after it: with floor 96 and ceiling 384,
    ``[300, 50, 50, 340]`` packs to ``[350, 50, 340]`` that way, and to
    ``[300, 100, 340]`` when the short span is the one choosing. Ties go left so
    the partition is deterministic.

    Best effort. A short span between two spans already at the ceiling cannot
    move, because nothing re-splits the result; removing those would take
    boundary redistribution, not merging.
    """
    if floor <= 0 or len(spans) <= 1:
        return spans
    pending = [(span, budget.count(text[span[0] : span[1]])) for span in spans]
    merged: list[tuple[tuple[int, int], int]] = []
    i = 0
    while i < len(pending):
        (start, end), count = pending[i]
        if count < floor:
            left = right = None
            if merged:
                span = (merged[-1][0][0], end)
                total = budget.count(text[span[0] : span[1]])
                if total <= budget.body:
                    left = (span, total, merged[-1][1])
            if i + 1 < len(pending):
                span = (start, pending[i + 1][0][1])
                total = budget.count(text[span[0] : span[1]])
                if total <= budget.body:
                    right = (span, total, pending[i + 1][1])
            if left is not None and (right is None or left[2] <= right[2]):
                merged[-1] = (left[0], left[1])
                i += 1
                continue
            if right is not None:
                pending[i + 1] = (right[0], right[1])
                i += 1
                continue
        merged.append(((start, end), count))
        i += 1
    return [span for span, _ in merged]


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
    except (ImportError, IndexError, SyntaxError, ValueError, RecursionError):
        # ``IndexError``: a parser that counts lines differently from the
        # ``\n``-built table above can index past it. Semantic boundaries are
        # optional — the token ceiling and the lossless partition are not — so
        # degrade to no structure rather than failing the file.
        symbols, statements = [], set()
    # Partition every source character exactly once, including decorators,
    # comments, imports and module constants that symbol-only parsers omitted.
    cuts = sorted({0, len(text), *(s.start for s in symbols), *(s.end for s in symbols)})
    spans: list[tuple[int, int]] = []
    for begin, finish in zip(cuts, cuts[1:]):
        boundaries = tuple(sorted(p - begin for p in statements if begin < p < finish))
        spans.extend(
            (begin + left, begin + right)
            for left, right in budget.spans(text[begin:finish], boundaries)
        )
    # Keep inter-symbol whitespace without indexing a separate blank result.
    # Only merge when the exact body budget still fits.
    packed: list[tuple[int, int]] = []
    for start, end in spans:
        if (
            packed
            and not text[start:end].strip()
            and budget.count(text[packed[-1][0] : end]) <= budget.body
        ):
            packed[-1] = (packed[-1][0], end)
        else:
            packed.append((start, end))
    # The section partition cuts at every symbol boundary and ``spans`` packs
    # greedily from the left, so gaps between symbols, the tail of a symbol body
    # and the head of an inner symbol each become their own chunk — often one or
    # two lines, identical across files, and dominated by their heading prefix
    # once embedded. The registry chunker path gets the same treatment from
    # ``_merge_short_chunks``; this path used to skip it entirely.
    #
    # The floor runs before the packing goal, not after: packing is greedy from
    # the left, so it reproduces exactly the stranding the floor pass exists to
    # avoid. With floor 96 and ceiling 384, packing ``[300, 50, 50, 340]`` first
    # gives ``[350, 50, 340]``, and the stranded 50 no longer fits either
    # neighbour. Floor first gives ``[300, 100, 340]``, which packing leaves
    # alone. Packing only ever grows spans, so it cannot reintroduce a short one.
    ceiling = min(config.max_chunk_tokens or budget.body, budget.body)
    packed = _merge_short_code_spans(text, packed, budget, config.min_chunk_tokens)
    packed = _pack_code_spans(text, packed, budget, config.target_chunk_tokens, ceiling)
    result: list[Chunk] = []
    for start, end in packed:
        enclosing = _enclosing_symbols(symbols, start, end)
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
        result.append(
            Chunk(
                content=text[start:end],
                metadata=ChunkMetadata(
                    source_file=path,
                    heading_hierarchy=(path.stem, *(s.name for s in enclosing)),
                    chunk_type=kind,
                    start_line=bisect.bisect_right(lines, start),
                    end_line=bisect.bisect_right(lines, max(start, end - 1)),
                    source_read_only=(
                        (start > 0 and text[start - 1] != "\n")
                        or (end < len(text) and text[end - 1] != "\n")
                    ),
                    language="python"
                    if path.suffix == ".py"
                    else ("typescript" if path.suffix in {".ts", ".tsx"} else "javascript"),
                ),
            )
        )
    from collections import Counter

    totals = Counter(c.metadata.heading_hierarchy for c in result)
    seen: Counter[tuple[str, ...]] = Counter()
    for (start, end), chunk in zip(packed, result):
        hierarchy = chunk.metadata.heading_hierarchy
        seen[hierarchy] += 1
        nearby = text[max(0, start - 2000) : start].splitlines()[-6:]
        comments = "\n".join(
            line.strip() for line in nearby if line.lstrip().startswith(("#", "//", "/*", "*"))
        )
        # Position-independent for the reason spelled out in ``bound_chunks``:
        # a line range in the embedded description turns any edit above a chunk
        # into a re-embed of everything below it.
        identity = (
            " > ".join(hierarchy)
            + f"\nFile: {path.name}\n"
            + f"Fragment {seen[hierarchy]}/{totals[hierarchy]}"
        )
        # Identity first; reserve room for local comments and concise symbol docs.
        detail_limit = max(1, budget.context // 4)
        details = [budget.trim(comments, detail_limit)]
        # Same containment rule as the hierarchy above: a merged chunk that
        # outgrew an inner symbol must not carry that symbol's signature either.
        for symbol in reversed(_enclosing_symbols(symbols, start, end)):
            details.extend(
                (budget.trim(symbol.signature, detail_limit), budget.trim(symbol.doc, detail_limit))
            )
        budget.describe(chunk, "\n".join(filter(None, [identity, *details])))
    return result


@dataclass(frozen=True)
class _JsonMember:
    start: int
    value_start: int
    end: int
    segment: str
    token_count: int


def _json_member_anchor(member: _JsonMember, size: int, floor: int, target: int) -> bool:
    """Choose a cut from a member segment, weighted by its token size."""
    if target <= 0:
        return True
    window = max(1, target - floor)
    digest = hashlib.blake2b(member.segment.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % window < size


def _pack_json_siblings(
    text: str,
    members: list[_JsonMember],
    budget: TokenBudget,
    floor: int,
    target: int,
    ceiling: int,
) -> list[tuple[int, int]]:
    """Pack siblings with content-defined cuts and an exact hard ceiling."""

    def tokens(start: int, end: int) -> int:
        return budget.count(text[members[start].start : members[end - 1].end])

    # A fixed-size greedy pack shifts every later boundary after an insertion.
    # Object keys select stable anchors, so a changed prefix converges at the
    # next anchor. Array segments are indices: inserting at the front shifts
    # their anchors and range labels, as it did with per-element JSON pointers.
    # Anchors may cut below target, trading extra chunks for stable object
    # identities. The floor delays early cuts; the ceiling forces a cut.
    groups: list[list[int]] = []
    start = 0
    end = 0
    while end < len(members):
        candidate = end + 1
        candidate_tokens = tokens(start, candidate)
        if end > start and candidate_tokens > ceiling:
            groups.append([start, end])
            start = end
            continue
        end = candidate
        if candidate_tokens >= floor and _json_member_anchor(
            members[end - 1], members[end - 1].token_count, floor, target
        ):
            groups.append([start, end])
            start = end
    if start < end:
        groups.append([start, end])

    # A final short group may need a boundary shifted from its neighbour, not
    # a wholesale merge. Keep both resulting bodies inside the exact ceiling.
    index = 0
    while floor > 0 and index < len(groups):
        start, end = groups[index]
        if tokens(start, end) >= floor:
            index += 1
            continue
        if index and tokens(groups[index - 1][0], end) <= ceiling:
            groups[index - 1][1] = end
            groups.pop(index)
            index = max(0, index - 1)
            continue
        if index + 1 < len(groups) and tokens(start, groups[index + 1][1]) <= ceiling:
            groups[index + 1][0] = start
            groups.pop(index)
            continue
        if index:
            previous = groups[index - 1]
            for boundary in range(previous[1] - 1, previous[0], -1):
                if (
                    floor <= tokens(previous[0], boundary) <= ceiling
                    and floor <= tokens(boundary, end) <= ceiling
                ):
                    previous[1] = boundary
                    groups[index][0] = boundary
                    break
        index += 1
    return [(start, end) for start, end in groups]


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

    def emit(pos: int, end: int, body: str, label: str, *, grouped: bool = False) -> None:
        chunks.append(
            Chunk(
                content=body,
                metadata=ChunkMetadata(
                    source_file=path,
                    heading_hierarchy=(path.stem, label),
                    start_line=text.count("\n", 0, pos) + 1,
                    end_line=text.count("\n", 0, end) + 1,
                    retrieval_context=(
                        f"JSON members: {label}" if grouped else f"JSON pointer: {label}"
                    ),
                ),
            )
        )

    def visit(pos: int, pointer: str, depth: int = 0) -> int:
        if depth > 32:
            raise ValueError("deep JSON requires lossless fallback")
        pos = whitespace(pos)
        value, end = decoder.raw_decode(text, pos)
        raw = text[pos:end]
        if budget.count(raw) > budget.body and isinstance(value, (dict, list)) and value:
            cursor = pos + 1
            members: list[_JsonMember] = []
            for index in range(len(value)):
                cursor = whitespace(cursor)
                member_start = cursor
                if isinstance(value, dict):
                    key, cursor = decoder.raw_decode(text, cursor)
                    cursor = whitespace(cursor)
                    if text[cursor] != ":":
                        raise ValueError("invalid JSON member")
                    cursor += 1
                    segment = str(key).replace("~", "~0").replace("/", "~1")
                else:
                    segment = str(index)
                value_start = whitespace(cursor)
                _, member_end = decoder.raw_decode(text, value_start)
                members.append(
                    _JsonMember(
                        member_start,
                        value_start,
                        member_end,
                        segment,
                        budget.count(text[member_start:member_end]),
                    )
                )
                cursor = whitespace(member_end)
                if index + 1 < len(value):
                    if text[cursor] != ",":
                        raise ValueError("invalid JSON separator")
                    cursor += 1
            ceiling = min(config.max_chunk_tokens or budget.body, budget.body)
            run: list[_JsonMember] = []

            def emit_run(group: list[_JsonMember]) -> None:
                if not group:
                    return
                for first, last in _pack_json_siblings(
                    text,
                    group,
                    budget,
                    config.min_chunk_tokens,
                    config.target_chunk_tokens,
                    ceiling,
                ):
                    if last == first + 1:
                        member = group[first]
                        visit(member.value_start, pointer + "/" + member.segment, depth + 1)
                    else:
                        first_member, last_member = group[first], group[last - 1]
                        # The heading is a displayed range descriptor, not a
                        # JSON pointer. JSON quoting keeps punctuation in keys
                        # and parent paths unambiguous to readers and tools.
                        label = json.dumps(
                            {
                                "parent": pointer or "/",
                                "first": first_member.segment,
                                "last": last_member.segment,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        emit(
                            first_member.start,
                            last_member.end,
                            text[first_member.start : last_member.end],
                            label,
                            grouped=True,
                        )

            for member in members:
                if member.token_count <= ceiling:
                    run.append(member)
                else:
                    emit_run(run)
                    run = []
                    visit(member.value_start, pointer + "/" + member.segment, depth + 1)
            emit_run(run)
        else:
            body = value if isinstance(value, str) and budget.count(raw) > budget.body else raw
            emit(pos, end, body, pointer or "/")
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
    bounded = bound_chunks(chunks, config, preserve_source_lines=serialized)
    for chunk in bounded:
        chunk.metadata = replace(chunk.metadata, source_read_only=True, source_span_hash=None)
    return bounded


def validate_budget_configuration(config: Any, previous: Any = None) -> None:
    """Validate before opening storage/models or publishing live configuration."""
    from memtomem.config import IndexingConfig

    if not isinstance(getattr(config, "indexing", None), IndexingConfig):
        return  # Compatibility with callers supplying only unrelated runtime knobs.
    if previous is not None and not isinstance(getattr(previous, "indexing", None), IndexingConfig):
        previous = None
    # Reach for ``.embedding`` through ``getattr`` on both sides: the guard two
    # lines up only establishes that each object carries an ``indexing``
    # section, and this function's contract explicitly admits callers that
    # supply "only unrelated runtime knobs".
    previous_embedding = getattr(previous, "embedding", None)
    config_embedding = getattr(config, "embedding", None)
    if (
        previous_embedding is not None
        and config_embedding is not None
        and any(
            getattr(previous_embedding, key, default) != getattr(config_embedding, key, default)
            for key, default in (("onnx_variant", "fp32"), ("onnx_artifact_path", ""))
        )
    ):
        raise ValueError("Embedding artifact changes require a Core restart")
    fields = (
        "chunk_input_prefix",
        "watcher_debounce_ms",
        "watcher_max_wait_ms",
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
    budget = TokenBudget(config.indexing)  # fail before any component/state mutation
    from memtomem.embedding.profiles import E5_TOKENIZER_SHA256, is_e5

    if config.embedding.provider.lower() == "onnx" and is_e5(config.embedding.model):
        if budget.fingerprint != E5_TOKENIZER_SHA256:
            raise ValueError("E5 chunk tokenizer checksum differs from its pinned model tokenizer")
    if (
        config.embedding.provider.lower() == "onnx"
        and config.embedding.max_sequence_tokens > 0
        and config.embedding.max_sequence_tokens < config.indexing.chunk_model_tokens
    ):
        raise ValueError("chunk_model_tokens exceeds ONNX max_sequence_tokens")
