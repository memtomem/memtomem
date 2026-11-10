"""Bounded descriptions without the ``File:`` and ``Fragment`` lines (#3209).

Bounded chunking writes each chunk's file name and fragment ordinal into its
retrieval description, which is embedded and BM25-indexed as content. A caller
whose file names are its own bookkeeping turns the lines off with
``create_components(chunk_source_labels=False)``.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from memtomem.chunking.bounded import TokenBudget, bound_chunks, chunk_code, chunk_json
from memtomem.config import IndexingConfig, Mem2MemConfig
from memtomem.models import Chunk, ChunkMetadata
from memtomem.runtime import close_components, create_components

LABEL_PREFIXES = ("File:", "Fragment")


def _byte_tokenizer(tmp_path: Path) -> str:
    """A tiny byte-level tokenizer file, as ``test_bounded_chunks`` builds one."""
    tokenizers = pytest.importorskip("tokenizers")
    alphabet = sorted(tokenizers.pre_tokenizers.ByteLevel.alphabet())
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.BPE(vocab={c: i for i, c in enumerate(alphabet)}, merges=[])
    )
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.ByteLevel(add_prefix_space=False)
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    return str(path)


def _bounded(tmp_path: Path) -> dict:
    return {
        "hard_max_chunk_tokens": 64,
        "chunk_tokenizer_path": _byte_tokenizer(tmp_path),
        "chunk_context_tokens": 256,
        "chunk_model_tokens": 384,
    }


@pytest.fixture
def config(tmp_path):
    return IndexingConfig(**_bounded(tmp_path))


def _markdown_chunk(path: Path) -> Chunk:
    body = "apples and pears " * 12  # several fragments at 64 byte tokens
    return Chunk(
        content=body,
        metadata=ChunkMetadata(
            source_file=path,
            heading_hierarchy=("Fruit", "Orchard"),
            start_line=1,
            end_line=1,
        ),
    )


def _labels(chunk: Chunk) -> list[str]:
    return [
        line
        for line in chunk.metadata.retrieval_context.splitlines()
        if line.startswith(LABEL_PREFIXES)
    ]


def _assert_within_budget(chunks: list[Chunk], config: IndexingConfig) -> None:
    budget = TokenBudget(config)
    for chunk in chunks:
        assert budget.count(chunk.content) <= config.hard_max_chunk_tokens
        assert budget.count(chunk.retrieval_content, special=True) <= config.chunk_model_tokens


# ---- the chunkers -------------------------------------------------------------


def test_bound_chunks_default_description_is_unchanged(config, tmp_path):
    # Pins today's text: a changed default would re-embed every existing
    # bounded index.
    chunks = bound_chunks([_markdown_chunk(tmp_path / "notes.md")], config)
    assert len(chunks) > 1
    assert [c.metadata.retrieval_context for c in chunks] == [
        f"Fruit > Orchard\nFile: notes.md\nFragment {i}/{len(chunks)}"
        for i in range(1, len(chunks) + 1)
    ]


def test_bound_chunks_without_labels_keeps_the_heading_path_only(config, tmp_path):
    with_labels = bound_chunks([_markdown_chunk(tmp_path / "notes.md")], config)
    chunks = bound_chunks([_markdown_chunk(tmp_path / "notes.md")], config, source_labels=False)
    assert [c.content for c in chunks] == [c.content for c in with_labels]
    assert [c.metadata.retrieval_context for c in chunks] == ["Fruit > Orchard"] * len(chunks)
    # Fragments no longer differ by description; their storage keys still do.
    keys = [(c.content_hash, c.metadata.start_line) for c in chunks]
    assert len(set(keys)) == len(keys)
    _assert_within_budget(chunks, config)


def test_bound_chunks_without_labels_or_headings_has_no_description(config, tmp_path):
    chunk = _markdown_chunk(tmp_path / "notes.md")
    chunk.metadata = replace(chunk.metadata, heading_hierarchy=())
    chunks = bound_chunks([chunk], config, source_labels=False)
    assert [c.metadata.retrieval_context for c in chunks] == [""] * len(chunks)
    assert all("notes" not in c.retrieval_content for c in chunks)


_CODE = "".join(f"def f{i}(x):\n    return x + {i}  # adds {i}\n\n\n" for i in range(12))


def test_chunk_code_default_description_is_unchanged(config, tmp_path):
    chunks = chunk_code(tmp_path / "tool.py", _CODE, config)
    assert chunks
    for chunk in chunks:
        identity = chunk.metadata.retrieval_context.split("\n")[:3]
        assert identity[1] == "File: tool.py"
        assert identity[2].startswith("Fragment ")


def test_chunk_code_without_labels(config, tmp_path):
    with_labels = chunk_code(tmp_path / "tool.py", _CODE, config)
    chunks = chunk_code(tmp_path / "tool.py", _CODE, config, source_labels=False)
    assert [c.content for c in chunks] == [c.content for c in with_labels]
    assert chunks and all(_labels(c) == [] for c in chunks)
    _assert_within_budget(chunks, config)


_JSON = '{"items": [' + ", ".join(f'{{"name": "item{i}", "n": {i}}}' for i in range(30)) + "]}"


def test_chunk_json_default_description_carries_labels(config, tmp_path):
    chunks = chunk_json(tmp_path / "data.json", _JSON, config)
    assert chunks and all(
        _labels(c)[:1] == ["File: data.json"] and _labels(c)[1].startswith("Fragment ")
        for c in chunks
    )


def test_chunk_json_without_labels(config, tmp_path):
    with_labels = chunk_json(tmp_path / "data.json", _JSON, config)
    chunks = chunk_json(tmp_path / "data.json", _JSON, config, source_labels=False)
    assert [c.content for c in chunks] == [c.content for c in with_labels]
    assert chunks and all(_labels(c) == [] for c in chunks)
    # The JSON chunker names the file in its heading path on its own; the
    # switch covers only the two label lines.
    assert all(c.metadata.heading_hierarchy[0] == "data" for c in chunks)


# ---- through create_components ------------------------------------------------


async def _open(tmp_path: Path, **kwargs):
    mem = tmp_path / "orchard"
    mem.mkdir(exist_ok=True)
    cfg = Mem2MemConfig.without_environment(
        storage={"sqlite_path": tmp_path / "index.db"},
        indexing={"memory_dirs": [mem], **_bounded(tmp_path)},
    )
    return mem, await create_components(cfg, load_ambient_config=False, **kwargs)


def _content_matches(comp, word: str) -> int:
    # The content column alone: on this branch's default the BM25 leg also
    # falls back to the path column, which is not what this checks.
    return (
        comp.storage._get_db()
        .execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (f"{{content}} : {word}",)
        )
        .fetchone()[0]
    )


def _contexts(comp) -> list[str]:
    rows = comp.storage._get_db().execute("SELECT retrieval_context FROM chunks ORDER BY rowid")
    return [r[0] for r in rows]


@pytest.mark.parametrize(
    ("labels", "expected"),
    [(True, {"file": 1, "fragment": 1, "md": 1, "sme": 1}), (False, {})],
    ids=["labels-on", "labels-off"],
)
async def test_components_switch_reaches_the_fts_content(tmp_path, labels, expected):
    mem, comp = await _open(tmp_path, chunk_source_labels=labels)
    try:
        (mem / "sme.md").write_text("## Fruit\n\nI like apples and pears.\n", encoding="utf-8")
        await comp.index_engine.index_path(mem)
        assert _contexts(comp)
        measured = {w: _content_matches(comp, w) for w in ("file", "fragment", "md", "sme")}
        assert measured == {w: expected.get(w, 0) for w in measured}
        assert _content_matches(comp, "pears") == 1
    finally:
        await close_components(comp)


@pytest.mark.parametrize(
    ("name", "text"),
    [("tool.py", _CODE), ("data.json", _JSON)],
    ids=["code", "json"],
)
async def test_the_engine_passes_the_switch_to_every_bounded_chunker(tmp_path, name, text):
    mem, comp = await _open(tmp_path, chunk_source_labels=False)
    try:
        (mem / name).write_text(text, encoding="utf-8")
        await comp.index_engine.index_path(mem)
        contexts = _contexts(comp)
        assert contexts
        assert [
            line for c in contexts for line in c.splitlines() if line.startswith(LABEL_PREFIXES)
        ] == []
    finally:
        await close_components(comp)


@pytest.mark.parametrize("first", [True, False], ids=["on-then-off", "off-then-on"])
async def test_an_unchanged_source_is_rechunked_when_the_switch_flips(tmp_path, first):
    # The index receipt lets an unchanged source skip chunking; it must not
    # carry descriptions written under the other value.
    mem, comp = await _open(tmp_path, chunk_source_labels=first)
    try:
        (mem / "sme.md").write_text("## Fruit\n\nI like apples and pears.\n", encoding="utf-8")
        await comp.index_engine.index_path(mem)
        assert any("File: sme.md" in c for c in _contexts(comp)) is first
        comp.index_engine._chunk_source_labels = not first
        await comp.index_engine.index_path(mem)
        assert any("File: sme.md" in c for c in _contexts(comp)) is (not first)
    finally:
        await close_components(comp)
