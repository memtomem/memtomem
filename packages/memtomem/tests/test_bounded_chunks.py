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
    storage._chunk_budget_config = bounded_config
    good = Chunk(content="good", metadata=ChunkMetadata(source_file=Path("good.md")))
    bad = Chunk(content="x" * 1000, metadata=ChunkMetadata(source_file=Path("bad.md")))
    with pytest.raises(ValueError, match="body exceeds"):
        await storage.upsert_chunks([good, bad])
    assert await storage.get_chunk(good.id) is None
    assert await storage.get_chunk(bad.id) is None


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


def test_packed_code_descriptions_match_final_lines(bounded_config):
    text = "def one():\n    return 1\n\n\ndef two():\n    return 2\n"
    chunks = chunk_code(Path("spans.py"), text, bounded_config)
    assert "".join(c.content for c in chunks) == text
    for chunk in chunks:
        meta = chunk.metadata
        assert f"Lines: {meta.start_line}-{meta.end_line};" in meta.retrieval_context


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
async def test_crlf_code_is_preserved_during_file_index(storage, bounded_config, tmp_path):
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
    assert "".join(c.content for c in chunks).encode() == source
    assert path.read_bytes() == source
