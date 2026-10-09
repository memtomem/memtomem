"""Chunk line ranges count ``\\n`` lines, whatever other separators a file holds.

``str.splitlines`` also breaks on U+2028, U+0085, form feed and others, so a
file holding one placed every later chunk low and rewrote the character to
``\\n`` in chunk content (#2699).
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from memtomem.chunking.base import physical_line_numbers, split_source_lines
from memtomem.config import IndexingConfig
from memtomem.indexing.engine import IndexEngine

SEPARATORS = pytest.mark.parametrize(
    "sep", ["\u2028", "\u0085", "\x0c"], ids=["u2028", "u0085", "formfeed"]
)


def _chunk(name: str, text: str, **config):
    indexing = IndexingConfig(min_chunk_tokens=0, **config)
    return IndexEngine(None, None, indexing).chunk_content(Path(name), text)


def _spans(chunks):
    return [(c.metadata.start_line, c.metadata.end_line) for c in chunks]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n",
        "\r\n",
        "\r\n\r\n",
        "a",
        "a\n",
        "a\nb",
        "a\r\nb",
        "a\r\nb\r\n",
        "\n\n",
        "  \n \n",
        "a\r\n\nb",
    ],
)
def test_lf_and_crlf_text_splits_as_splitlines_does(text):
    """Ordinary files keep their numbering, content and hashes."""
    assert split_source_lines(text) == text.splitlines()
    assert split_source_lines(text, keepends=True) == text.splitlines(keepends=True)
    assert physical_line_numbers(text) == list(range(1, len(text.splitlines()) + 1))


@SEPARATORS
def test_split_source_lines_breaks_on_newline_only(sep):
    text = f"a{sep}b\nc"
    assert split_source_lines(text) == [f"a{sep}b", "c"]
    assert "".join(split_source_lines(text, keepends=True)) == text
    assert physical_line_numbers(text) == [1, 1, 2]


@SEPARATORS
def test_markdown_sections_sit_on_their_lines(sep):
    text = f"# A\n\nx{sep}# H\n\n# B\n\nz\n"
    chunks = _chunk("a.md", text)
    assert _spans(chunks) == [(1, 4), (5, 7)]
    # The text after the separator is not a heading: CommonMark ends lines at \n.
    assert [c.metadata.heading_hierarchy for c in chunks] == [("# A",), ("# B",)]
    assert chunks[0].content == f"x{sep}# H"


@SEPARATORS
def test_markdown_fence_lines_count_the_same_lines_as_headings(sep):
    """Fence lines numbered on other lines than headings skipped a real heading."""
    text = f"# A\n\nx{sep}y\n```\n# not heading\n```\n# Real\n\nbody\n"
    chunks = _chunk("a.md", text)
    assert [c.metadata.heading_hierarchy for c in chunks] == [("# A",), ("# Real",)]
    assert _spans(chunks) == [(1, 6), (7, 9)]


@SEPARATORS
def test_markdown_paragraphs_open_no_fence_mid_line(sep):
    from memtomem.chunking.markdown import _split_paragraphs_fence_aware

    assert _split_paragraphs_fence_aware(f"intro{sep}```\nbody\n\nnext") == [
        f"intro{sep}```\nbody",
        "next",
    ]


@SEPARATORS
def test_markdown_single_line_part_splits_at_sentences(sep):
    """A part on one ``\\n`` line has no line boundaries to cut at."""
    from memtomem.chunking.markdown import _split_oversized_part

    spans = _split_oversized_part(f"One. Two{sep}three four five six.", 18)
    assert [(s.start, s.end) for s in spans] == [(0, 5), (5, 20), (20, 29)]


@SEPARATORS
def test_markdown_blockquote_tags_start_a_line(sep):
    """A ``> tags:`` after a separator sits inside the line above, not on its own."""
    from memtomem.chunking.markdown import MarkdownChunker

    text = f"> created: now{sep}> tags: [fake]\n\nbody"
    assert MarkdownChunker()._extract_section_blockquote_tags(text) == ([], text, 0)


@SEPARATORS
def test_markdown_oversized_section_keeps_a_fence_after_a_separator_in_text(sep):
    """Backticks after a separator do not start a line, so they open no fence.

    Read as a fence, they made the oversized part atomic: one chunk carried every
    paragraph below.
    """
    body = "\n\n".join(f"paragraph {i} " + "word " * 30 for i in range(12))
    text = f"# A\n\nintro{sep}```\nnot a fence\n\n{body}\n"
    chunks = _chunk("big.md", text, max_chunk_tokens=120, target_chunk_tokens=120)
    assert len(chunks) > 1, "the fixture must split"
    first = text.split("\n").index("not a fence") + 1
    holder = next(c for c in chunks if "not a fence" in c.content)
    assert f"intro{sep}```" in holder.content
    assert holder.metadata.start_line <= first <= holder.metadata.end_line
    assert "paragraph 11" not in holder.content


@SEPARATORS
def test_rst_sections_sit_on_their_lines(sep):
    text = f"A\n==\n\nalpha{sep}beta\n\nB\n==\n\ngamma\n"
    chunks = _chunk("a.rst", text)
    assert _spans(chunks) == [(1, 5), (6, 9)]
    assert f"alpha{sep}beta" in chunks[0].content


@SEPARATORS
@pytest.mark.parametrize(
    ("name", "text", "spans", "second"),
    [
        (
            "a.py",
            "def a():\n    s = 'x{S}y'\n    return s\n\n\ndef b():\n    return 2\n",
            [(1, 3), (6, 7)],
            "def b():",
        ),
        (
            "a.js",
            "function a() {{\n  const s = 'x{S}y';\n  return s;\n}}\n\n"
            "function b() {{\n  return 2;\n}}\n",
            [(1, 4), (6, 8)],
            "function b() {",
        ),
    ],
    ids=["python", "javascript"],
)
def test_code_bodies_come_from_the_parser_rows(sep, name, text, spans, second):
    """Tree-sitter rows count ``\\n`` lines; the body is sliced from the same lines."""
    chunks = _chunk(name, text.format(S=sep))
    assert _spans(chunks) == spans
    assert sep in chunks[0].content
    assert chunks[1].content.startswith(second)


@pytest.mark.parametrize("sep", ["\u2028", "\u0085"], ids=["u2028", "u0085"])
@pytest.mark.parametrize(
    ("name", "text", "spans"),
    [
        ("a.yaml", "one:\n  k: 'x{S}y'\ntwo:\n  k: 2\n", [(1, 2), (3, 4)]),
        ("a.json", '{{\n  "one": "x{S}y",\n  "two": 2\n}}\n', [(2, 2), (3, 4)]),
    ],
    ids=["yaml", "json"],
)
def test_structured_keys_sit_on_their_lines(sep, name, text, spans):
    # Form feed is left out: both parsers reject it, and the file falls back
    # to one serialised chunk at (0, 0).
    chunks = _chunk(name, text.format(S=sep))
    assert _spans(chunks) == spans
    assert sep in chunks[0].content


@pytest.mark.parametrize("sep", ["\u2028", "\u0085"], ids=["u2028", "u0085"])
def test_yaml_keys_split_by_a_separator_report_the_line_they_share(sep):
    """PyYAML breaks on these separators, so ``two`` is a key on line 1."""
    chunks = _chunk("b.yaml", f"one: a{sep}two: b\nthree: c\n")
    assert [c.metadata.heading_hierarchy[-1] for c in chunks] == ["one", "two", "three"]
    assert _spans(chunks) == [(1, 1), (1, 1), (2, 2)]


@pytest.mark.parametrize("name", ["a.yaml", "a.json"])
def test_structured_input_its_parser_rejects_falls_back_unplaced(name):
    """Both parsers reject a raw form feed, so the file is one serialised chunk."""
    text = {
        "a.yaml": "one:\n  k: 'x\x0cy'\ntwo:\n  k: 2\n",
        "a.json": '{\n  "one": "x\x0cy",\n  "two": 2\n}\n',
    }[name]
    assert _spans(_chunk(name, text)) == [(0, 0)]


@pytest.fixture
def hard_budget(tmp_path):
    """A byte-level tokenizer, so the exact budget splits small fixtures."""
    tokenizers = pytest.importorskip("tokenizers")
    alphabet = sorted(tokenizers.pre_tokenizers.ByteLevel.alphabet())
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.BPE(vocab={c: i for i, c in enumerate(alphabet)}, merges=[])
    )
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.ByteLevel(add_prefix_space=False)
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    return {
        "hard_max_chunk_tokens": 64,
        "chunk_tokenizer_path": str(path),
        "chunk_context_tokens": 256,
        "chunk_model_tokens": 384,
    }


@SEPARATORS
def test_fragments_under_a_hard_budget_sit_on_their_lines(sep, hard_budget):
    text = (
        "# A\n\nx" + sep + "y\n\n" + "".join(f"body line {i:02d} with words\n" for i in range(12))
    )
    chunks = _chunk("a.md", text, max_chunk_tokens=2000, **hard_budget)
    assert len(chunks) > 2, "the fixture must split"
    source = text.split("\n")
    for chunk in chunks:
        meta = chunk.metadata
        window = source[meta.start_line - 1 : meta.end_line]
        for line in chunk.content.split("\n"):
            if line.startswith("body line"):
                assert line in window, (meta.start_line, meta.end_line, line)


def test_identical_fragments_on_one_line_keep_distinct_storage_keys():
    """Several logical lines of one value now share a ``\\n`` line.

    Identical fragments there shared ``(content_hash, start_line)``: an insert
    kept one and a line-range refresh failed on the unique index.
    """
    text = '{"big": "prefix\u2028' + ("x" * 31 + "\u2028") * 3 + 'tail"}\n'
    chunks = _chunk(
        "big.json", text, max_chunk_tokens=10, target_chunk_tokens=10, chunk_overlap_tokens=0
    )
    keys = [(c.content_hash, c.metadata.start_line) for c in chunks]
    assert len(keys) == len(set(keys))
    assert any(c.content == "x" * 31 + "\u2028" for c in chunks), "the fixture must repeat"


async def test_reindex_moves_identical_fragments_onto_one_line(components, memory_dir, monkeypatch):
    """An index built with the old numbering refreshes onto the new one.

    The old numbering stored the fragments on distinct lines; the refresh moved
    each onto the one line they share and failed on the unique index.
    """
    from unittest.mock import AsyncMock

    import memtomem.chunking.structured as structured
    import memtomem.indexing.engine as engine_module

    embedder = AsyncMock()
    embedder.embed_texts = AsyncMock(side_effect=lambda texts, **_: [[0.1] * 1024 for _ in texts])
    embedder.dimension = 1024
    engine = components.index_engine
    engine._embedder = embedder
    engine._config.max_chunk_tokens = 10
    engine._config.min_chunk_tokens = 0
    engine._config.chunk_overlap_tokens = 0
    path = memory_dir / "big.json"
    path.write_text(
        '{"big": "prefix\u2028' + ("x" * 31 + "\u2028") * 3 + 'tail"}\n', encoding="utf-8"
    )

    # The release that indexed it numbered splitlines lines; the upgrade's new
    # version stops its receipt from being reused, as a real upgrade does.
    with monkeypatch.context() as patch:
        patch.setattr(
            structured,
            "physical_line_numbers",
            lambda text: list(range(1, len(text.splitlines()) + 1)),
        )
        patch.setattr(engine_module, "_memtomem_version", "0.0.0-before-2699")
        await engine.index_file(path)
    before = await components.storage.list_chunks_by_source(path)
    assert len({c.metadata.start_line for c in before}) > 1, "the old numbering must spread"
    embedder.embed_texts.reset_mock()

    await engine.index_file(path)
    after = await components.storage.list_chunks_by_source(path)
    assert {c.metadata.start_line for c in after} == {1}
    keys = [(c.content_hash, c.metadata.start_line) for c in after]
    assert len(keys) == len(set(keys))
    # Every fragment kept its text, so the refresh moved rows without embedding.
    embedder.embed_texts.assert_not_awaited()


# Calls left on ``str.splitlines``, each with the reason it is not a line
# position in the file.
_KEPT_SPLITLINES = {
    # Maps splitlines lines onto \n lines; the conversion itself.
    ("base.py", "physical_line_numbers"): 1,
    # The last few lines above a symbol, for its retrieval description.
    ("bounded.py", "chunk_code"): 1,
    # Frontmatter values, a separate decision from line positions.
    ("markdown.py", "_extract_frontmatter_tags"): 1,
    ("markdown.py", "_extract_validity_window"): 1,
    # Key detection on the lines PyYAML reads; numbers go through
    # physical_line_numbers.
    ("structured.py", "_chunk_original"): 1,
    ("structured.py", "_find_key_lines"): 1,
}


def test_chunkers_split_source_lines_on_newline_only():
    root = Path(__file__).parents[1] / "src" / "memtomem" / "chunking"
    found: Counter[tuple[str, str]] = Counter()
    for path in sorted(root.glob("*.py")):
        for func in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "splitlines"
                ):
                    found[(path.name, func.name)] += 1
    assert dict(found) == _KEPT_SPLITLINES
