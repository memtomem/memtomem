"""Short-fragment merging in the bounded code chunker (issue #2475).

``chunk_code`` partitions a file at every symbol boundary and packs each section
greedily, so gaps between symbols, symbol-body tails and inner-symbol heads used
to be stored as one- and two-line chunks. These pin the merge that removes them,
and the invariants it must not break: a contiguous character partition, the
exact token ceiling, and per-span line ranges, hierarchy and rewrite flags.
"""

import pytest

from memtomem.chunking.bounded import TokenBudget, chunk_code
from memtomem.config import IndexingConfig
from memtomem.models import ChunkType


@pytest.fixture
def e5_like_config(tmp_path):
    """A byte-level tokenizer, so one token is one byte and sizes are exact."""
    tokenizers = pytest.importorskip("tokenizers")
    alphabet = sorted(tokenizers.pre_tokenizers.ByteLevel.alphabet())
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.BPE(vocab={char: index for index, char in enumerate(alphabet)}, merges=[])
    )
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.ByteLevel(add_prefix_space=False)
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    return IndexingConfig(
        hard_max_chunk_tokens=384,
        chunk_tokenizer_path=str(path),
        chunk_context_tokens=96,
        chunk_model_tokens=512,
        max_chunk_tokens=384,
        target_chunk_tokens=320,
        min_chunk_tokens=96,
    )


def function_source(name: str, size: int) -> str:
    """A syntactically valid function whose source is about ``size`` bytes."""
    body = "a" * max(1, size - len(f"def {name}():\n    x = ''\n"))
    return f"def {name}():\n    x = '{body}'\n"


def assert_partition(chunks, text):
    """The chunks are a contiguous, byte-exact partition of the decoded text.

    Checked on character offsets, not line ranges: ``start_line``/``end_line``
    are inclusive, so two chunks that share a line legitimately overlap there.
    """
    assert "".join(chunk.content for chunk in chunks) == text


def assert_budget(chunks, config):
    budget = TokenBudget(config)
    for chunk in chunks:
        assert budget.count(chunk.content) <= config.hard_max_chunk_tokens
        assert budget.count(chunk.retrieval_content, special=True) <= config.chunk_model_tokens


def test_short_gap_between_functions_is_absorbed(e5_like_config, tmp_path):
    text = function_source("first", 200) + "drive(scenario())\n" + function_source("second", 200)
    chunks = chunk_code(tmp_path / "sample.py", text, e5_like_config)

    assert_partition(chunks, text)
    assert_budget(chunks, e5_like_config)
    assert not [c for c in chunks if c.content == "drive(scenario())\n"]
    budget = TokenBudget(e5_like_config)
    assert all(budget.count(c.content) >= e5_like_config.min_chunk_tokens for c in chunks)


def test_short_span_prefers_the_smaller_neighbour(e5_like_config, tmp_path):
    """The ordering regression, measured: sections are 300/50/50/340 tokens.

    Folding each short span into whichever neighbour is smaller gives
    ``[300, 100, 340]``. Two rules break it, and both leave a stranded 50 that
    fits neither ``350 + 50`` nor ``50 + 340`` under the 384 ceiling: packing
    towards the target before enforcing the floor, and always folding left.
    """
    text = (
        function_source("first", 300)
        + function_source("t1", 50)
        + function_source("t2", 50)
        + function_source("second", 340)
    )
    chunks = chunk_code(tmp_path / "sample.py", text, e5_like_config)

    assert_partition(chunks, text)
    assert_budget(chunks, e5_like_config)
    budget = TokenBudget(e5_like_config)
    assert [budget.count(c.content) for c in chunks] == [300, 100, 340]


def test_the_floor_pass_is_not_redundant_with_packing(e5_like_config, tmp_path):
    """Packing alone leaves the 50-token span stranded; the floor pass does not.

    Without this the suite stays green with ``_merge_short_code_spans`` removed:
    for an ordinary short gap the packing pass absorbs it anyway.
    """
    text = (
        function_source("first", 300)
        + function_source("t1", 50)
        + function_source("t2", 50)
        + function_source("second", 340)
    )
    off = e5_like_config.model_copy(update={"min_chunk_tokens": 0})
    budget = TokenBudget(e5_like_config)
    packed_only = [budget.count(c.content) for c in chunk_code(tmp_path / "s.py", text, off)]

    assert packed_only == [350, 50, 340]


def test_inner_symbol_tail_is_absorbed(e5_like_config, tmp_path):
    """``return wrapped`` after an inner ``def`` is its own section today."""
    text = (
        "def decorate(fn):\n"
        "    @wraps(fn)\n"
        "    def wrapped(*args):\n"
        f"        value = '{'a' * 150}'\n"
        "        return fn(*args)\n"
        "    return wrapped\n"
    )
    chunks = chunk_code(tmp_path / "sample.py", text, e5_like_config)

    assert_partition(chunks, text)
    assert "    return wrapped\n" not in [c.content for c in chunks]


def test_file_below_the_floor_stays_one_chunk(e5_like_config, tmp_path):
    text = "import os\n"
    chunks = chunk_code(tmp_path / "sample.py", text, e5_like_config)

    assert [c.content for c in chunks] == [text]


def test_whitespace_only_file_yields_no_chunks(e5_like_config, tmp_path):
    assert chunk_code(tmp_path / "sample.py", "\n\n   \n", e5_like_config) == []


def test_merging_is_opt_out(e5_like_config, tmp_path):
    """``min_chunk_tokens=0`` with no packing goal leaves the partition alone."""
    text = function_source("first", 200) + "drive(scenario())\n" + function_source("second", 200)
    off = e5_like_config.model_copy(update={"min_chunk_tokens": 0, "target_chunk_tokens": 0})
    chunks = chunk_code(tmp_path / "sample.py", text, off)

    assert_partition(chunks, text)
    assert "drive(scenario())\n" in [c.content for c in chunks]


def test_merged_chunk_reports_the_symbol_that_contains_all_of_it(e5_like_config, tmp_path):
    """A chunk that outgrew its symbol must not keep that symbol's name."""
    text = function_source("first", 200) + "drive(scenario())\n"
    chunks = chunk_code(tmp_path / "sample.py", text, e5_like_config)

    assert_partition(chunks, text)
    merged = [c for c in chunks if "drive(scenario())" in c.content]
    assert merged
    for chunk in merged:
        assert chunk.metadata.heading_hierarchy == ("sample",)
        assert chunk.metadata.chunk_type == ChunkType.RAW_TEXT
        assert "def first" not in (chunk.metadata.retrieval_context or "")


def test_line_ranges_follow_the_merged_content(e5_like_config, tmp_path):
    text = function_source("first", 200) + "drive(scenario())\n" + function_source("second", 200)
    chunks = chunk_code(tmp_path / "sample.py", text, e5_like_config)

    offset = 0
    for chunk in chunks:
        expected_start = text.count("\n", 0, offset) + 1
        offset += len(chunk.content)
        expected_end = text.count("\n", 0, max(0, offset - 1)) + 1
        assert (chunk.metadata.start_line, chunk.metadata.end_line) == (
            expected_start,
            expected_end,
        )


def test_line_aligned_chunks_stay_writable(e5_like_config, tmp_path):
    text = function_source("first", 200) + "drive(scenario())\n" + function_source("second", 200)
    chunks = chunk_code(tmp_path / "sample.py", text, e5_like_config)

    assert not any(c.metadata.source_read_only for c in chunks)


def test_a_mid_line_boundary_survives_the_merge(e5_like_config, tmp_path):
    """Recomputing the flag must not hand out rewrite authority it should not.

    In *Python* a mid-line boundary only ever comes from a ceiling split —
    ``_python_structure`` keys every symbol to ``offset(lineno - 1)``, so section
    cuts land on line starts — and re-merging across a ceiling split necessarily
    breaks the ceiling again. Measured over 941 repository ``.py`` files, no
    chunk is read-only either before or after the change. That is a fact about
    this parser, not about merging: see
    ``test_typescript_merging_can_restore_whole_line_ownership``.

    A 900-character line splits into two full chunks and a 139-token tail. The
    tail is over the floor, the 18-token header before it is under the floor and
    cannot merge (18 + 384 > 384), and every piece carved out of the long line
    keeps its partial-line boundary.
    """
    text = "def f():\n    pass\n" + "x = '" + "a" * 900 + "'\n"
    chunks = chunk_code(tmp_path / "sample.py", text, e5_like_config)

    assert_partition(chunks, text)
    assert [c.metadata.source_read_only for c in chunks] == [False, True, True, True]


def test_typescript_merging_can_restore_whole_line_ownership(e5_like_config, tmp_path):
    """The other direction, which the Python parser never reaches.

    tree-sitter starts a symbol after ``export ``, so offset 7 is a cut in the
    middle of line 1 and both pieces own a partial line. Merging them back gives
    a chunk that owns whole lines, and recomputing the flag — rather than OR-ing
    the inputs, which could only keep it set — reports that correctly.

    Measured: with both passes off the spans are 7/22 tokens, both read-only;
    with them on, one 29-token chunk that is not.
    """
    pytest.importorskip("tree_sitter")
    text = "export function f(): void {}\n"
    off = e5_like_config.model_copy(update={"min_chunk_tokens": 0, "target_chunk_tokens": 0})

    unmerged = chunk_code(tmp_path / "sample.ts", text, off)
    assert [c.metadata.source_read_only for c in unmerged] == [True, True]

    chunks = chunk_code(tmp_path / "sample.ts", text, e5_like_config)
    assert_partition(chunks, text)
    assert [c.metadata.source_read_only for c in chunks] == [False]


def test_typescript_path_merges_too(e5_like_config, tmp_path):
    """The fragments must exist with merging off, or this pins nothing.

    Measured: with both passes disabled the spans are 7/292/8/42/8/42/8/333
    tokens, five of them under the 96-token floor.
    """
    pytest.importorskip("tree_sitter")
    from memtomem.chunking.bounded import _javascript_structure

    def ts_function(name: str, size: int) -> str:
        head = f"export function {name}(): string {{\n  return '';\n}}\n"
        return f"export function {name}(): string {{\n  return '{'a' * max(1, size - len(head))}';\n}}\n"

    text = (
        ts_function("first", 300)
        + ts_function("t1", 50)
        + ts_function("t2", 50)
        + ts_function("second", 340)
    )
    symbols, _ = _javascript_structure(tmp_path / "sample.ts", text)
    assert [s.name for s in symbols] == ["first", "t1", "t2", "second"]

    budget = TokenBudget(e5_like_config)
    off = e5_like_config.model_copy(update={"min_chunk_tokens": 0, "target_chunk_tokens": 0})
    unmerged = [budget.count(c.content) for c in chunk_code(tmp_path / "sample.ts", text, off)]
    assert min(unmerged) < e5_like_config.min_chunk_tokens

    chunks = chunk_code(tmp_path / "sample.ts", text, e5_like_config)
    assert_partition(chunks, text)
    assert_budget(chunks, e5_like_config)
    assert all(budget.count(c.content) >= e5_like_config.min_chunk_tokens for c in chunks)


def test_a_span_between_two_full_ones_is_left_alone(e5_like_config, tmp_path):
    """Best-effort, and the limit is pinned rather than claimed away.

    Two sections at the ceiling with a short one between them cannot merge in
    either direction without breaking the exact ceiling, and nothing re-splits
    the result. Removing these needs boundary redistribution.
    """
    text = function_source("first", 384) + "drive()\n" + function_source("second", 384)
    chunks = chunk_code(tmp_path / "sample.py", text, e5_like_config)

    assert_partition(chunks, text)
    assert_budget(chunks, e5_like_config)
    budget = TokenBudget(e5_like_config)
    assert [c.content for c in chunks if budget.count(c.content) < 96] == ["drive()\n"]
