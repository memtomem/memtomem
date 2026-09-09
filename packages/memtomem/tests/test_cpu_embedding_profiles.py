"""CPU profile contracts and completed-generation reuse regressions."""

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from memtomem.config import EmbeddingConfig, Mem2MemConfig, IndexingConfig
from memtomem.embedding.profiles import E5_TOKENIZER
from memtomem.indexing.watcher import FileWatcher, _STOP_SENTINEL


@pytest.mark.parametrize("provider", ["onnx", "ONNX"])
def test_default_e5_budget_contract(provider):
    config = Mem2MemConfig(embedding=EmbeddingConfig(provider=provider))
    assert config.embedding.model == "multilingual-e5-small"
    assert (config.embedding.dimension, config.embedding.max_sequence_tokens) == (384, 512)
    assert (config.embedding.threads, config.embedding.onnx_batch_size) == (2, 4)
    assert config.indexing.chunk_tokenizer_path == E5_TOKENIZER
    assert config.indexing.chunk_input_prefix == "passage: "
    assert config.indexing.hard_max_chunk_tokens == 384


def test_existing_bge_config_is_preserved():
    config = Mem2MemConfig(
        embedding=EmbeddingConfig(provider="onnx", model="bge-m3", dimension=1024)
    )
    assert config.embedding.dimension == 1024
    assert not config.indexing.chunk_input_prefix


@pytest.mark.parametrize("override", [{"dimension": 1024}, {"max_sequence_tokens": 0}])
def test_e5_rejects_incompatible_model_contract(override):
    with pytest.raises(ValueError):
        EmbeddingConfig(provider="onnx", **override)


async def test_continuous_events_cannot_starve_maximum_wait():
    watcher = FileWatcher(Mock(), IndexingConfig(), debounce_ms=100)
    watcher._max_wait_s = 0.04
    flushed = asyncio.Event()

    async def flush(paths):
        flushed.set()
        return set()

    watcher._flush_batch = flush
    task = asyncio.create_task(watcher._process_events())

    async def produce():
        # Keep input continuous until cancellation. Sub-millisecond timer
        # rounding on Windows can exhaust a finite producer before the deadline.
        while True:
            await watcher._queue.put(Path("/tmp/updated.md"))
            await asyncio.sleep(0)

    producer = asyncio.create_task(produce())
    try:
        await asyncio.wait_for(flushed.wait(), 2)
        assert not producer.done()
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
        await asyncio.wait_for(watcher._queue.put(_STOP_SENTINEL), 1)
        await asyncio.wait_for(task, 2)


async def test_receipt_skips_chunker_but_changed_source_invalidates(
    bm25_only_components, monkeypatch
):
    comp, directory = bm25_only_components
    source = directory / "receipt.md"
    source.write_text("# Receipt\n\nAn ordinary nonsecret memory.\n")
    assert not (await comp.index_engine.index_file(source)).errors
    original = comp.index_engine.chunk_content
    chunker = Mock(wraps=original)
    monkeypatch.setattr(comp.index_engine, "chunk_content", chunker)
    result = await comp.index_engine.index_file(source)
    assert not result.errors
    chunker.assert_not_called()
    source.write_text("# Receipt\n\nA changed ordinary memory.\n")
    assert not (await comp.index_engine.index_file(source)).errors
    chunker.assert_called_once()


def test_late_bge_override_does_not_inherit_generated_e5_budgets(tmp_path, monkeypatch):
    import json
    import memtomem.config as module
    from memtomem.embedding.profiles import apply_e5_defaults

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"embedding": {"model": "bge-m3", "dimension": 1024}}))
    monkeypatch.setattr(module, "_CONFIG_OVERRIDE_PATH", path)
    config = Mem2MemConfig(embedding=EmbeddingConfig(provider="onnx"))
    module.load_config_overrides(config, migrate=False)
    apply_e5_defaults(config)
    assert config.embedding.model == "bge-m3"
    assert config.embedding.max_sequence_tokens == 1024
    assert config.indexing.hard_max_chunk_tokens == 0
    assert not config.indexing.chunk_input_prefix


def test_late_e5_override_keeps_unspecified_profile_defaults(tmp_path, monkeypatch):
    import json
    import memtomem.config as module
    from memtomem.embedding.profiles import apply_e5_defaults

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {"embedding": {"provider": "onnx"}, "indexing": {"memory_dirs": [str(tmp_path)]}}
        )
    )
    monkeypatch.setattr(module, "_CONFIG_OVERRIDE_PATH", path)
    config = Mem2MemConfig()
    module.load_config_overrides(config, migrate=False)
    apply_e5_defaults(config)
    assert config.embedding.dimension == 384
    assert config.embedding.max_sequence_tokens == 512
    assert config.indexing.chunk_tokenizer_path == E5_TOKENIZER


async def test_receipt_invalidates_when_stored_metadata_changes(bm25_only_components, monkeypatch):
    comp, directory = bm25_only_components
    source = directory / "receipt-state.md"
    source.write_text("# Receipt\n\nAn ordinary memory.\n")
    assert not (await comp.index_engine.index_file(source)).errors
    comp.storage._get_db().execute(
        "UPDATE chunks SET source_read_only=1 WHERE source_file=?", (str(source),)
    )
    comp.storage._get_db().commit()
    chunker = Mock(wraps=comp.index_engine.chunk_content)
    monkeypatch.setattr(comp.index_engine, "chunk_content", chunker)
    assert not (await comp.index_engine.index_file(source)).errors
    chunker.assert_called_once()


async def test_e5_roles_are_added_on_both_public_embedding_paths(monkeypatch):
    from memtomem.embedding.onnx import OnnxEmbedder

    embedder = OnnxEmbedder(EmbeddingConfig(provider="onnx"))
    observed = []

    def capture(texts, *args, **kwargs):
        observed.extend(texts)
        return [[0.0] * 384 for _ in texts]

    monkeypatch.setattr(embedder, "_embed_sync", capture)
    try:
        await embedder.embed_texts(["한국어 문서"])
        await embedder.embed_query("한국어 질문")
        assert observed == ["passage: 한국어 문서", "query: 한국어 질문"]
    finally:
        await embedder.close()


def test_artifact_switch_requires_restart():
    from memtomem.chunking.bounded import validate_budget_configuration

    old = Mem2MemConfig()
    candidate = old.model_copy(deep=True)
    candidate.embedding.onnx_variant = "int8-arm64"
    candidate.embedding.onnx_artifact_path = "/unused-artifact"
    with pytest.raises(ValueError, match="restart"):
        validate_budget_configuration(candidate, old)


def test_quantized_e5_budget_uses_local_artifact_tokenizer(tmp_path):
    config = Mem2MemConfig(
        embedding=EmbeddingConfig(
            provider="onnx", onnx_variant="int8-arm64", onnx_artifact_path=str(tmp_path)
        )
    )
    assert config.indexing.chunk_tokenizer_path == str(tmp_path / "tokenizer.json")
    assert config.indexing.chunk_input_prefix == "passage: "


def test_local_artifact_validation_needs_no_hub_cache(tmp_path, monkeypatch):
    import hashlib
    import json
    from memtomem.embedding import profiles

    (tmp_path / "model.onnx").write_bytes(b"fixture-model")
    (tmp_path / "tokenizer.json").write_bytes(b"fixture-tokenizer")
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    manifest = {
        "schema": 1,
        "model": profiles.E5_MODEL,
        "revision": profiles.E5_REVISION,
        "precision": "int8",
        "variant": "int8-arm64",
        "files": hashes,
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(profiles.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(profiles, "E5_TOKENIZER_SHA256", hashes["tokenizer.json"])

    def no_download(*args):
        raise AssertionError("Local artifact must not require a separate Hub cache")

    monkeypatch.setattr(profiles, "resolve_tokenizer", no_download)
    profiles.artifact_manifest(str(tmp_path), profiles.E5_MODEL, "int8-arm64")
    (tmp_path / "model.onnx").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        profiles.artifact_manifest(str(tmp_path), profiles.E5_MODEL, "int8-arm64")
