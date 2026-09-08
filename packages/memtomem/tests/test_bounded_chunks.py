"""Regressions for exact limits, original source spans and retrieval identity."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from memtomem.chunking.bounded import TokenBudget, bound_chunks, chunk_code, chunk_json
from memtomem.config import IndexingConfig
from memtomem.indexing.chunk_context import enrich_context
from memtomem.indexing.differ import compute_diff
from memtomem.indexing.engine import IndexEngine
from memtomem.models import Chunk, ChunkMetadata
from memtomem.tools.export_import import _chunk_to_dict, _dict_to_chunk
from memtomem.web.schemas.core import chunk_to_out


@pytest.fixture
def bounded_config(tmp_path):
    tokenizers = pytest.importorskip("tokenizers")
    alphabet = sorted(tokenizers.pre_tokenizers.ByteLevel.alphabet())
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.BPE(
            vocab={char: index for index, char in enumerate(alphabet)},
            merges=[],
        )
    )
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.ByteLevel(add_prefix_space=False)
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    return IndexingConfig(
        hard_max_chunk_tokens=64,
        chunk_tokenizer_path=str(path),
        chunk_context_tokens=256,
        chunk_model_tokens=384,
    )


def assert_bounded(chunks, config):
    budget = TokenBudget(config)
    assert chunks
    for chunk in chunks:
        assert budget.count(chunk.content) <= config.hard_max_chunk_tokens
        assert budget.count(chunk.metadata.retrieval_context) <= config.chunk_context_tokens
        assert budget.count(chunk.retrieval_content, special=True) <= config.chunk_model_tokens


@pytest.mark.parametrize(
    "text",
    [
        "긴 한글 문장🙂 e\u0301 " * 100,
        "```python\n" + "x = '" + "a" * 1000 + "'\n```",
        "| long | row |\n|---|---|\n|" + "cells " * 500 + "|",
        "- " + "a single bullet " * 400,
        "\n" * 1000,
    ],
)
def test_lossless_all_text_boundaries(text, bounded_config):
    source = Chunk(
        content=text,
        metadata=ChunkMetadata(
            source_file=Path("note.md"),
            start_line=5,
            heading_hierarchy=("a very long heading " * 100,),
        ),
    )
    chunks = bound_chunks([source], bounded_config)
    assert "".join(c.content for c in chunks) == text
    assert_bounded(chunks, bounded_config)
    offset = 0
    for chunk in chunks:
        assert chunk.metadata.start_line == 5 + text.count("\n", 0, offset)
        offset += len(chunk.content)


def test_code_keeps_gaps_decorators_comments_and_unicode_names(bounded_config):
    text = (
        "# module context\nimport os\nFLAG = 1\n\n"
        'class 계산기:\n    """Calculate totals."""\n'
        '    @staticmethod\n    def 더하기(값):\n        """Add the input."""\n'
        "        # preserve all statements\n"
        + "        값 += 1\n" * 40
        + "        return 값\n\n# trailing comment\n"
    )
    chunks = chunk_code(Path("math.py"), text, bounded_config)
    assert "".join(c.content for c in chunks) == text
    assert_bounded(chunks, bounded_config)
    methods = [c for c in chunks if "더하기" in c.metadata.heading_hierarchy]
    assert len(methods) > 1
    assert all(c.metadata.heading_hierarchy == ("math", "계산기", "더하기") for c in methods)
    assert any("Add the input" in c.metadata.retrieval_context for c in methods)
    offset = 0
    for chunk in chunks:
        assert chunk.metadata.start_line == text.count("\n", 0, offset) + 1
        offset += len(chunk.content)
        assert chunk.metadata.end_line == text.count("\n", 0, offset - 1) + 1


def test_parser_failure_still_lossless_and_bounded(bounded_config):
    text = "def broken(:\n" + "문자열" * 300
    chunks = chunk_code(Path("broken.py"), text, bounded_config)
    assert "".join(c.content for c in chunks) == text
    assert_bounded(chunks, bounded_config)


@pytest.mark.parametrize("suffix", [".js", ".ts", ".tsx", ".jsx"])
def test_js_ts_unicode_and_fallback(suffix, bounded_config):
    text = "// 주석\nclass 계산기 { 더하기(값) {\n" + "값 += 1;\n" * 60 + "return 값; } }\n"
    chunks = chunk_code(Path("module" + suffix), text, bounded_config)
    assert "".join(c.content for c in chunks) == text
    assert_bounded(chunks, bounded_config)
    if any(c.metadata.chunk_type.value != "raw_text" for c in chunks):
        assert any("더하기" in c.metadata.heading_hierarchy for c in chunks)


def test_json_long_scalar_decoded_with_real_source_span(bounded_config):
    import json

    payload = "첫 줄\nsecond line\n" * 100
    text = json.dumps({"metadata": "example", "a/b~c": payload}, ensure_ascii=False, indent=2)
    chunks = chunk_json(Path("fixture.json"), text, bounded_config)
    values = [c for c in chunks if c.metadata.heading_hierarchy[-1] == "/a~1b~0c"]
    assert "".join(c.content for c in values) == payload
    assert all(c.metadata.start_line == c.metadata.end_line == 3 for c in values)
    assert_bounded(chunks, bounded_config)


def test_json_array_and_invalid_input(bounded_config):
    import json

    text = json.dumps(["word " * 40, {"next": "한글 " * 40}])
    chunks = chunk_json(Path("records.json"), text, bounded_config)
    assert any(c.metadata.heading_hierarchy[-1] == "/1/next" for c in chunks)
    assert_bounded(chunks, bounded_config)
    invalid = '{ "a": ' + "broken" * 100
    fallback = chunk_json(Path("invalid.json"), invalid, bounded_config)
    assert "".join(c.content for c in fallback) == invalid
    assert_bounded(fallback, bounded_config)


def test_engine_final_budget_after_overlap(bounded_config):
    config = bounded_config.model_copy(update={"chunk_overlap_tokens": 20})
    engine = IndexEngine(None, None, config)
    chunks = engine.chunk_content(Path("note.md"), "# Title\n\n" + "a " * 3000)
    assert_bounded(chunks, config)


def test_context_change_reembeds_but_preserves_body_id():
    old = Chunk(
        content="return total",
        metadata=ChunkMetadata(
            source_file=Path("code.py"),
            retrieval_context="old purpose",
        ),
    )
    new = Chunk(
        content=old.content, metadata=replace(old.metadata, retrieval_context="new purpose")
    )
    state = {str(old.id): (old.content_hash, (), (), None, None, "old purpose")}
    diff = compute_diff(state, [new])
    assert diff.to_upsert == [new]
    assert new.id == old.id and new.content_hash == old.content_hash
    assert not diff.to_delete and not diff.metadata_only


@pytest.mark.asyncio
async def test_storage_export_api_and_rebuild_preserve_context(storage):
    original = Chunk(
        content="plain body",
        metadata=ChunkMetadata(
            source_file=Path("/tmp/description.md"),
            retrieval_context="contextuniqueterm",
        ),
    )
    await storage.upsert_chunks([original])
    stored = (await storage.get_chunks_batch([original.id]))[original.id]
    assert stored.retrieval_content == original.retrieval_content
    assert chunk_to_out(stored).retrieval_context == "contextuniqueterm"
    imported, _ = _dict_to_chunk(_chunk_to_dict(stored))
    assert imported.retrieval_content == stored.retrieval_content
    before = await storage.bm25_search("contextuniqueterm", 10)
    assert before
    await storage.rebuild_fts()
    assert await storage.bm25_search("contextuniqueterm", 10)
    stored.metadata = replace(stored.metadata, retrieval_context="replacementterm")
    await storage.upsert_chunks([stored])
    assert not await storage.bm25_search("contextuniqueterm", 10)
    assert await storage.bm25_search("replacementterm", 10)


@pytest.mark.asyncio
async def test_optional_llm_cache_dependency_and_failure(bounded_config, storage):
    class LLM:
        generate = AsyncMock(return_value="Adds the input.")

    llm = LLM()
    chunks = chunk_code(Path("code.py"), "def add(x):\n    return x + 1\n", bounded_config)
    await enrich_context(chunks, bounded_config, llm, storage)
    llm.generate.assert_not_called()
    await storage.upsert_chunks(chunks)
    config = bounded_config.model_copy(update={"enrich_chunk_context": True})
    await enrich_context(chunks, config, llm, storage)
    calls = llm.generate.call_count
    fresh = chunk_code(Path("code.py"), "def add(x):\n    return x + 1\n", config)
    await enrich_context(fresh, config, llm, storage)
    assert llm.generate.call_count == calls
    assert fresh[0].retrieval_content == chunks[0].retrieval_content
    changed = chunk_code(Path("code.py"), "def add(x):\n    return x + 2\n", config)
    structural = changed[0].metadata.retrieval_context
    llm.generate.side_effect = TimeoutError
    await enrich_context(changed, config, llm, storage)
    assert llm.generate.call_count > calls
    assert changed[0].metadata.retrieval_context == structural


@pytest.mark.asyncio
async def test_storage_rejects_entire_oversized_batch(storage, bounded_config):
    """A refusal here is a ``StorageError``, the error every caller maps.

    A bare ``ValueError`` escaped the whole call chain — auto-tag, dedup and
    the consolidation engine all re-upsert chunks and none of them catch one —
    and inside ``storage.transaction()`` it aborted the surrounding group.
    """
    from memtomem.errors import StorageError

    storage._chunk_budget_config = bounded_config
    good = Chunk(content="good", metadata=ChunkMetadata(source_file=Path("good.md")))
    bad = Chunk(content="x" * 1000, metadata=ChunkMetadata(source_file=Path("bad.md")))
    with pytest.raises(StorageError, match="body exceeds") as raised:
        await storage.upsert_chunks([good, bad])
    # The refused chunk is named, not buried under "transaction rolled back".
    assert str(bad.id) in str(raised.value)
    assert await storage.get_chunk(good.id) is None
    assert await storage.get_chunk(bad.id) is None


@pytest.mark.asyncio
async def test_a_body_already_stored_can_still_be_written_back(storage, bounded_config):
    """Turning a budget on over an existing DB leaves oversized rows behind.

    That is the documented migration (``mm index --force`` converts them), so a
    caller changing only tags or a namespace has to be able to write such a row
    back. Validating every body on every upsert made those rows permanently
    unwritable, and auto-tag, dedup and consolidation all rebuild a Chunk from
    the stored content before re-upserting it.
    """
    from dataclasses import replace as replace_meta

    legacy = Chunk(content="x" * 1000, metadata=ChunkMetadata(source_file=Path("legacy.md")))
    await storage.upsert_chunks([legacy])  # no budget configured yet
    assert await storage.get_chunk(legacy.id) is not None

    storage._chunk_budget_config = bounded_config
    retagged = Chunk(
        id=legacy.id,
        content=legacy.content,
        metadata=replace_meta(legacy.metadata, tags=("added",)),
    )
    await storage.upsert_chunks([retagged])

    stored = await storage.get_chunk(legacy.id)
    assert stored is not None
    assert stored.metadata.tags == ("added",)

    # A *new* oversized body for that same id is still refused.
    from memtomem.errors import StorageError

    grown = Chunk(
        id=legacy.id,
        content="y" * 1200,
        metadata=legacy.metadata,
    )
    with pytest.raises(StorageError, match="body exceeds"):
        await storage.upsert_chunks([grown])


@pytest.mark.asyncio
async def test_grandfathering_compares_bodies_not_hashes(storage, bounded_config):
    """A matching ``content_hash`` does not mean a matching body.

    ``Chunk.content_hash`` is taken over NFC-normalised text, so a precomposed
    string and its decomposed twin hash identically while differing in length
    and in token count. Keying the exemption on the hash let a caller replace a
    grandfathered row with a *different, larger* body under an active ceiling —
    an honest recomputed hash, no tampering required.
    """
    from memtomem.errors import StorageError

    budget = TokenBudget(bounded_config)
    precomposed = "\u00e9" * 30  # é
    decomposed = "e\u0301" * 30  # e + combining acute
    assert precomposed != decomposed
    legacy = Chunk(content=precomposed, metadata=ChunkMetadata(source_file=Path("nfc.md")))
    swapped = Chunk(id=legacy.id, content=decomposed, metadata=legacy.metadata)
    assert legacy.content_hash == swapped.content_hash
    assert budget.count(legacy.content) < budget.count(swapped.content)

    await storage.upsert_chunks([legacy])
    storage._chunk_budget_config = bounded_config

    with pytest.raises(StorageError, match="body exceeds"):
        await storage.upsert_chunks([swapped])
    stored = await storage.get_chunk(legacy.id)
    assert stored is not None
    assert stored.content == precomposed


@pytest.mark.asyncio
async def test_only_a_body_that_cannot_pass_is_grandfathered(storage, bounded_config):
    """ "Unchanged" is not the test; "could never pass" is.

    A row written under the current budget also has an unchanged body on a
    tag-only rewrite. Waiving its composed-input check would let a caller push
    a valid row past the model ceiling by growing only its description, which is
    the opposite of what the exemption is for. So the fitting body gets the full
    check and only the oversized one is exempt.

    Asserted on the decision rather than on an overflow, because the config
    validator reserves headroom precisely to keep an overflow off the ordinary
    path — a test that waited for one would pin nothing.
    """
    from memtomem.chunking import bounded as bounded_module

    calls: list[tuple[str, str]] = []
    real_validate = bounded_module.TokenBudget.validate
    real_description = bounded_module.TokenBudget.validate_description

    def spy_validate(self, chunk):
        calls.append(("validate", chunk.content))
        return real_validate(self, chunk)

    def spy_description(self, chunk):
        calls.append(("validate_description", chunk.content))
        return real_description(self, chunk)

    fits = Chunk(content="x" * 10, metadata=ChunkMetadata(source_file=Path("fits.md")))
    oversized = Chunk(content="y" * 1000, metadata=ChunkMetadata(source_file=Path("big.md")))
    await storage.upsert_chunks([fits, oversized])
    storage._chunk_budget_config = bounded_config

    budget = TokenBudget(bounded_config)
    assert budget.count(fits.content) <= budget.body
    assert budget.count(oversized.content) > budget.body

    bounded_module.TokenBudget.validate = spy_validate
    bounded_module.TokenBudget.validate_description = spy_description
    try:
        await storage.upsert_chunks([fits, oversized])
    finally:
        bounded_module.TokenBudget.validate = real_validate
        bounded_module.TokenBudget.validate_description = real_description

    # ``validate`` delegates to ``validate_description``, so the fitting body
    # legitimately shows both; what distinguishes the two paths is whether the
    # full check ran at all.
    assert ("validate", fits.content) in calls, "a body that fits is fully checked"
    assert ("validate", oversized.content) not in calls, "the oversized body stays exempt"
    assert ("validate_description", oversized.content) in calls, (
        "and its description is still bounded"
    )


@pytest.mark.asyncio
async def test_a_grandfathered_body_cannot_be_grown_past_the_model_ceiling(storage, bounded_config):
    """The body exemption waives the per-chunk ceiling, never the model one.

    A body over the per-chunk limit can still compose well under the model
    limit, so exempting it there would let a caller push the row past the limit
    the budget exists to enforce by growing only its description — and the
    embedder would silently truncate its input. The escape hatch is a body that
    on its own exceeds the model budget: no description makes that fit, so
    demanding it would make the row unwritable, which is the failure the
    exemption is for.
    """
    from memtomem.errors import StorageError

    budget = TokenBudget(bounded_config)
    body = "z" * 127
    assert budget.count(body) > budget.body, "the fixture body must be grandfathered"

    legacy = Chunk(content=body, metadata=ChunkMetadata(source_file=Path("legacy.md")))
    await storage.upsert_chunks([legacy])
    storage._chunk_budget_config = bounded_config

    grown = Chunk(
        id=legacy.id,
        content=body,
        metadata=replace(
            legacy.metadata, retrieval_context="d" * bounded_config.chunk_context_tokens
        ),
    )
    # The description fits the context budget on its own; only the composition
    # overflows, which is the case the old condition let through.
    budget.validate_description(grown)
    assert budget.count(grown.retrieval_content, special=True) > budget.model

    with pytest.raises(StorageError, match="composed retrieval input exceeds"):
        await storage.upsert_chunks([grown])

    # The same row with a description that composes inside the budget is fine.
    modest = Chunk(
        id=legacy.id,
        content=body,
        metadata=replace(legacy.metadata, retrieval_context="short"),
    )
    await storage.upsert_chunks([modest])
    stored = await storage.get_chunk(legacy.id)
    assert stored is not None
    assert stored.metadata.retrieval_context == "short"


@pytest.mark.asyncio
async def test_a_body_larger_than_the_model_budget_stays_writable(storage, bounded_config):
    """The escape hatch: no description can make this body fit, so do not ask."""
    budget = TokenBudget(bounded_config)
    body = "z" * 800
    assert budget.count(body) > budget.model

    legacy = Chunk(content=body, metadata=ChunkMetadata(source_file=Path("huge.md")))
    await storage.upsert_chunks([legacy])
    storage._chunk_budget_config = bounded_config

    retagged = Chunk(
        id=legacy.id,
        content=body,
        metadata=replace(legacy.metadata, tags=("added",)),
    )
    await storage.upsert_chunks([retagged])
    stored = await storage.get_chunk(legacy.id)
    assert stored is not None
    assert stored.metadata.tags == ("added",)


@pytest.mark.asyncio
async def test_a_grandfathered_body_does_not_grandfather_a_new_description(storage, bounded_config):
    """The exemption is per component.

    An unchanged body waives the body and composed-input checks, because both
    are functions of that same body and would refuse it whatever the caller
    does. The description is text the caller chose on *this* write, so it stays
    bounded — otherwise a legacy row became a hole for an unbounded one.
    """
    from dataclasses import replace as replace_meta

    from memtomem.errors import StorageError

    legacy = Chunk(content="x" * 1000, metadata=ChunkMetadata(source_file=Path("legacy.md")))
    await storage.upsert_chunks([legacy])
    storage._chunk_budget_config = bounded_config

    oversized_description = Chunk(
        id=legacy.id,
        content=legacy.content,
        metadata=replace_meta(legacy.metadata, retrieval_context="d" * 4000),
    )
    with pytest.raises(StorageError, match="description exceeds"):
        await storage.upsert_chunks([oversized_description])

    # A description that fits is still accepted on the same grandfathered body.
    ok = Chunk(
        id=legacy.id,
        content=legacy.content,
        metadata=replace_meta(legacy.metadata, retrieval_context="short description"),
    )
    await storage.upsert_chunks([ok])
    stored = await storage.get_chunk(legacy.id)
    assert stored is not None
    assert stored.metadata.retrieval_context == "short description"


@pytest.mark.asyncio
async def test_embedding_failure_keeps_existing_file(components, bounded_config, tmp_path):
    config = bounded_config.model_copy(update={"memory_dirs": [tmp_path]})
    source = tmp_path / "safe.py"
    source.write_text('def add(x):\n    """Adds an input."""\n    return x + 1\n')
    embedder = AsyncMock()
    embedder.dimension = 1024
    embedder.embed_texts = AsyncMock(
        side_effect=lambda texts, **kwargs: [[0.1] * 1024 for _ in texts]
    )
    engine = IndexEngine(components.storage, embedder, config)
    await engine.index_file(source)
    original = await components.storage.get_chunk_index_state(source)
    assert original
    source.write_text('def add(x):\n    """Updated purpose."""\n' + "    x += 1\n" * 100)
    embedder.embed_texts.side_effect = RuntimeError("embedding unavailable")
    result = await engine.index_file(source, force=True)
    assert result.errors
    assert await components.storage.get_chunk_index_state(source) == original


@pytest.mark.asyncio
async def test_import_rejects_oversized_bundle_before_embedding(storage, bounded_config, tmp_path):
    from memtomem.tools.export_import import ExportBundle, import_chunks

    good = Chunk(content="small", metadata=ChunkMetadata(source_file=Path("good.md")))
    bad = Chunk(content="x" * 1000, metadata=ChunkMetadata(source_file=Path("large.md")))
    bundle = tmp_path / "bundle.json"
    bundle.write_text(ExportBundle(chunks=[_chunk_to_dict(good), _chunk_to_dict(bad)]).to_json())
    embedder = AsyncMock()
    with pytest.raises(ValueError, match="No records were imported"):
        await import_chunks(storage, embedder, bundle, indexing_config=bounded_config)
    embedder.embed_texts.assert_not_called()
    assert await storage.get_chunk(good.id) is None


@pytest.mark.asyncio
async def test_runtime_rejects_incompatible_onnx_truncation(bounded_config):
    from memtomem.config import EmbeddingConfig, Mem2MemConfig
    from memtomem.runtime.components import create_components

    config = Mem2MemConfig(
        indexing=bounded_config,
        embedding=EmbeddingConfig(
            provider="onnx",
            model="bge-m3",
            max_sequence_tokens=128,
        ),
    )
    with pytest.raises(ValueError, match="exceeds ONNX max_sequence_tokens"):
        await create_components(config, load_ambient_config=False)


def test_duplicate_json_keys_are_lossless(bounded_config):
    text = '{"same":"' + "apple " * 100 + '","same":"SECOND_RECORD"}'
    chunks = chunk_json(Path("duplicate.json"), text, bounded_config)
    assert "".join(c.content for c in chunks) == text
    assert_bounded(chunks, bounded_config)


def test_a_line_break_the_table_does_not_know_degrades_instead_of_failing(bounded_config):
    """``ast`` counts lines by universal-newline rules; the table counts ``\n``.

    A lone ``\r`` therefore gives ``ast`` more lines than the table has. The
    indexer reads with universal newlines so this cannot arrive through it, but
    a direct caller holding raw bytes can still get here, and losing the whole
    file to an ``IndexError`` over a parser detail is the wrong failure. Semantic
    boundaries are optional; the lossless partition and the token ceiling are not.
    """
    text = "def a():\r    return 1\r\rdef b():\r    return 2\r"
    assert "\n" not in text, "the fixture is only interesting without any \\n"

    chunks = chunk_code(Path("cr.py"), text, bounded_config)

    assert "".join(c.content for c in chunks) == text
    assert_bounded(chunks, bounded_config)


def test_packed_code_chunks_carry_the_final_line_range(bounded_config):
    """Packing merges trailing whitespace into the previous chunk.

    The merged chunk's ``end_line`` has to move with it, or the range a search
    reads points at the wrong lines. The range lives in the columns rather than
    in ``retrieval_context`` — putting it in the description made every chunk
    below an edit re-embed — so this reads the columns.
    """
    text = "def one():\n    return 1\n\n\ndef two():\n    return 2\n"
    chunks = chunk_code(Path("spans.py"), text, bounded_config)
    assert "".join(c.content for c in chunks) == text
    lines = text.count("\n")
    for chunk in chunks:
        meta = chunk.metadata
        assert 1 <= meta.start_line <= meta.end_line <= lines
        assert meta.retrieval_context
        # Position-independent: nothing in the description names a line.
        assert "Lines:" not in meta.retrieval_context
    # Contiguous cover: each chunk starts where the previous one ended.
    assert chunks[0].metadata.start_line == 1
    assert chunks[-1].metadata.end_line == lines


def test_long_input_never_tokenizes_an_unbounded_remainder(bounded_config, monkeypatch):
    budget = TokenBudget(bounded_config)
    observed = []
    original = budget.count

    def count(text, **kwargs):
        observed.append(len(text))
        return original(text, **kwargs)

    monkeypatch.setattr(budget, "count", count)
    text = "a" * 317_228
    spans = budget.spans(text)
    assert "".join(text[a:b] for a, b in spans) == text
    assert max(observed) <= 65_536


@pytest.mark.asyncio
async def test_description_cache_has_source_lifecycle(storage):
    path = Path("cache.py")
    await storage.set_chunk_descriptions(path, {"orphan": "must not persist"})
    assert await storage.get_chunk_descriptions(path) == {}
    chunk = Chunk(content="safe", metadata=ChunkMetadata(source_file=path))
    await storage.upsert_chunks([chunk])
    await storage.set_chunk_descriptions(path, {"old": "old description"})
    await storage.set_chunk_descriptions(path, {"new": "new description"})
    assert await storage.get_chunk_descriptions(path) == {"new": "new description"}
    await storage.delete_by_source(path)
    assert await storage.get_chunk_descriptions(path) == {}


def test_budget_changes_require_restart(bounded_config):
    from memtomem.config import Mem2MemConfig
    from memtomem.chunking.bounded import validate_budget_configuration

    old = Mem2MemConfig(indexing=bounded_config)
    new = old.model_copy(deep=True)
    new.indexing.chunk_context_tokens += 1
    with pytest.raises(ValueError, match="restart"):
        validate_budget_configuration(new, old)


@pytest.mark.asyncio
async def test_crlf_code_is_partitioned_losslessly_and_never_rewritten(
    storage, bounded_config, tmp_path
):
    """Chunks reassemble the file *as the indexer read it*, and the file is untouched.

    The indexer reads with universal newlines, which is what every downstream
    reader is specified over: ``chunking/markdown._FRONT_MATTER_RE`` and
    ``indexing/redaction_exemption`` both stop matching on ``\r``, so a
    byte-preserving read silently drops a CRLF note's tags, validity window and
    ``redaction:`` declaration. Losslessness is therefore a property of the
    decoded text — no character dropped between chunks — not of the raw bytes.
    """
    path = tmp_path / "crlf.py"
    source = b"def f():\r\n    return 1\r\n\r\n"
    path.write_bytes(source)
    embedder = AsyncMock()
    embedder.dimension = 0
    embedder.model_name = "none"
    config = bounded_config.model_copy(update={"memory_dirs": [tmp_path]})
    engine = IndexEngine(storage, embedder, config)
    await engine.index_file(path)
    chunks = await storage.list_chunks_by_source(path, limit=None)
    assert "".join(c.content for c in chunks) == path.read_text(encoding="utf-8")
    # The source file is never rewritten, whatever the indexer decoded.
    assert path.read_bytes() == source


@pytest.mark.asyncio
async def test_crlf_frontmatter_still_declares_and_still_tags(storage, bounded_config, tmp_path):
    """The regression a byte-preserving read caused, pinned end to end."""
    from memtomem.indexing.redaction_exemption import declared_exemption

    path = tmp_path / "note.md"
    body = "---\nvalid_from: 2024-01-01\ntags: [alpha]\n---\n# Title\n\nSome body text.\n"
    path.write_bytes(body.replace("\n", "\r\n").encode())
    embedder = AsyncMock()
    embedder.dimension = 0
    embedder.model_name = "none"
    config = bounded_config.model_copy(update={"memory_dirs": [tmp_path]})
    engine = IndexEngine(storage, embedder, config)
    await engine.index_file(path)

    chunks = await storage.list_chunks_by_source(path, limit=None)
    assert chunks
    assert any("alpha" in c.metadata.tags for c in chunks)
    assert any(c.metadata.valid_from_unix == 1704067200 for c in chunks)

    # The declaration reader sees the same text the chunker does.
    declared = "---\nredaction: documents-patterns\n---\n# T\n"
    assert declared_exemption(Path("x.md"), declared) == "documents-patterns"


def test_an_edit_above_a_chunk_does_not_change_the_chunks_below_it(bounded_config):
    """#1788's no-embed path must survive bounded chunking.

    ``compute_diff`` routes any hash-equal chunk whose ``retrieval_context``
    moved to ``to_upsert`` — a re-embed, and a re-run of the LLM enrichment
    whose cache is keyed on that same string. A description carrying the
    chunk's line range therefore made one inserted line re-embed every chunk
    below it, where the unbounded path refreshed the ranges and embedded
    nothing.
    """
    body = "".join(f"line {i} with enough words to split the body\n" for i in range(120))
    source = Chunk(
        content=body,
        metadata=ChunkMetadata(source_file=Path("notes.md"), start_line=1, end_line=120),
    )
    before = bound_chunks([source], bounded_config)
    assert len(before) > 3, "the fixture must actually split"

    shifted = Chunk(
        content="a new first line\n" + body,
        metadata=ChunkMetadata(source_file=Path("notes.md"), start_line=1, end_line=121),
    )
    after = bound_chunks([shifted], bounded_config)

    # Same bytes -> same description, so the differ sees no reason to embed.
    by_hash = {c.content_hash: c.metadata.retrieval_context for c in before}
    survivors = [c for c in after if c.content_hash in by_hash]
    assert survivors, "at least one body must survive the insertion unchanged"
    for chunk in survivors:
        assert chunk.metadata.retrieval_context == by_hash[chunk.content_hash]

    # And the line ranges still moved, which is what the cheap refresh writes.
    assert any(c.metadata.start_line != 1 for c in after)


def test_a_description_that_overflows_only_once_composed_is_trimmed(bounded_config):
    """Body and context each fit; the composed input is what the embedder sees.

    ``IndexingConfig`` cannot price the separator or the tokenizer's special
    tokens — it runs before any tokenizer is loaded — so a model whose overhead
    exceeds the constant reserve can compose past the budget from a body and a
    description that each fit. Failing there would fail the whole file for a
    description that is free to be shorter.

    The overflow is created by tightening ``model`` on the budget instance
    rather than by building an invalid config, because the config validator
    exists precisely to make that config unbuildable. The precondition is
    asserted, so this cannot quietly stop exercising the trimming branch.
    """
    budget = TokenBudget(bounded_config)
    body = "x" * bounded_config.hard_max_chunk_tokens
    description = "d" * (bounded_config.chunk_context_tokens * 4)
    trimmed = budget.trim(description, budget.context)

    # Precondition: with the description trimmed only against the *context*
    # budget, the composed input does not fit. Without this the test would pass
    # with the trimming branch deleted.
    budget.model = budget.count(trimmed) + budget.count(body)
    composed = budget.count(trimmed + "\n\n" + body, special=True)
    assert composed > budget.model, (composed, budget.model)

    chunk = Chunk(content=body, metadata=ChunkMetadata(source_file=Path("a.md")))
    budget.describe(chunk, description)

    assert chunk.metadata.retrieval_context, "the description must not be emptied"
    assert len(chunk.metadata.retrieval_context) < len(trimmed), "it must have been trimmed"
    assert budget.count(chunk.retrieval_content, special=True) <= budget.model
    # ``validate`` runs inside ``describe`` and must not have raised.
    budget.validate(chunk)


def test_the_configuration_reserves_room_for_composing(tmp_path):
    """The validator must refuse a split that only overflows once composed."""
    from memtomem.config import _COMPOSED_INPUT_RESERVE

    assert _COMPOSED_INPUT_RESERVE > 0

    def build(model: int) -> None:
        IndexingConfig(
            hard_max_chunk_tokens=100,
            chunk_tokenizer_path=str(tmp_path / "t.json"),
            chunk_context_tokens=_COMPOSED_INPUT_RESERVE,
            chunk_model_tokens=model,
        )

    body_plus_context = 100 + _COMPOSED_INPUT_RESERVE

    # The case that distinguishes this rule from the one it replaced. The old
    # check was ``body + context >= model``, which accepts this; the reserve is
    # the whole point, so a test that never builds a config in the gap pins
    # nothing.
    assert _COMPOSED_INPUT_RESERVE > 1, "the gap below needs room to exist"
    with pytest.raises(ValueError, match="chunk model budget must exceed"):
        build(body_plus_context + _COMPOSED_INPUT_RESERVE // 2)

    # Refused under both rules: body and context alone already fill the model.
    with pytest.raises(ValueError, match="chunk model budget must exceed"):
        build(body_plus_context)

    # Accepted: past the reserve.
    build(body_plus_context + _COMPOSED_INPUT_RESERVE + 1)
