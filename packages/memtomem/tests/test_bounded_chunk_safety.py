"""PR #2372: safe source rewrites, complete import scans and schema convergence."""

from dataclasses import replace
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

import test_bounded_chunks
from test_source_provenance import SURFACES, _request
from memtomem.config import StorageConfig
from memtomem.indexing.engine import IndexEngine
from memtomem.models import Chunk, ChunkMetadata
from memtomem.storage.sqlite_backend import SqliteBackend
from memtomem.tools.export_import import (
    ImportPrivacyError,
    _chunk_to_dict,
    _dict_to_chunk,
    export_chunks,
    import_chunks,
)


bounded_config = test_bounded_chunks.bounded_config


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "kind",
    ["markdown", "pre_split_markdown", "json", "javascript", "python", "unicode_lines", "masked"],
)
async def test_fragments_refuse_source_mutation(
    bm25_only_components, bounded_config, monkeypatch, surface, kind
):
    comp, directory = bm25_only_components
    bodies = {
        "markdown": ("note.md", "alpha beta gamma " * 60 + "\n"),
        "pre_split_markdown": ("note.md", "# Title\n\n" + "alpha beta gamma " * 600 + "\n"),
        "json": ("data.json", json.dumps({"body": "alpha beta gamma " * 60})),
        "javascript": ("code.js", "const f = () => { return '" + "a" * 1000 + "'; };\n"),
        "python": ("code.py", "VALUE = '" + "a" * 1000 + "'\n"),
        "unicode_lines": ("code.py", "VALUE = 'left\u2028right'\n"),
        "masked": ("masked.md", '# Example\n\n`"password": "example-value"`\n'),
    }
    if kind == "masked":
        from memtomem.indexing import privacy_projection

        monkeypatch.setattr(privacy_projection, "PROJECTION_ENABLED", True)
    filename, body = bodies[kind]
    source = directory / filename
    source.write_text(body)
    config = bounded_config.model_copy(update={"memory_dirs": [directory]})
    if kind == "pre_split_markdown":
        config = config.model_copy(
            update={"hard_max_chunk_tokens": 4096, "chunk_model_tokens": 8192}
        )
    embedder = AsyncMock()
    embedder.dimension = 1024
    embedder.model_name = "stub"
    embedder.supports_input_context = False
    embedder.embed_texts.side_effect = lambda texts, **kw: [[0.1] * 1024 for _ in texts]
    comp.index_engine = IndexEngine(comp.storage, embedder, config)
    stats = await comp.index_engine.index_file(source)
    assert not stats.errors
    chunks = await comp.storage.list_chunks_by_source(source, limit=None)
    if kind == "pre_split_markdown":
        assert len(chunks) > 1
        assert all(len(c.content) < config.hard_max_chunk_tokens for c in chunks)
        assert all(c.metadata.source_read_only for c in chunks)
    chunk = next(c for c in chunks if c.metadata.source_read_only)
    assert chunk.metadata.source_span_hash is None
    before = source.read_bytes()
    db = comp.storage._get_db()
    snapshots = {
        table: db.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
        for table in ("chunks", "chunks_fts", "chunks_vec")
    }
    result = await _request(comp, surface, chunk.id)
    if isinstance(result, str):
        assert "read_only" in result
    else:
        assert result.status_code == 409, result.text
        assert "read_only" in result.json()["detail"]
    assert source.read_bytes() == before
    for table, rows in snapshots.items():
        assert db.execute(f"SELECT * FROM {table}").fetchall() == rows  # noqa: S608


@pytest.mark.parametrize("surface", SURFACES)
async def test_unsplit_bounded_entry_keeps_original_range(
    bm25_only_components, bounded_config, surface
):
    comp, directory = bm25_only_components
    source = directory / "entry.md"
    source.write_text('## Entry\n\n> tags: ["keep"]\n\nsmall body\n')
    config = bounded_config.model_copy(update={"memory_dirs": [directory]})
    comp.index_engine = IndexEngine(comp.storage, comp.embedder, config)
    assert not (await comp.index_engine.index_file(source)).errors
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    assert chunk.metadata.end_line == 5
    assert not chunk.metadata.source_read_only
    assert chunk.metadata.source_span_hash
    result = await _request(comp, surface, chunk.id)
    if isinstance(result, str):
        assert result.startswith("Memory "), result
    else:
        assert result.status_code == 200, result.text
    if surface.endswith("edit"):
        assert 'tags: ["keep"]' in source.read_text()
        assert "NEW BODY" in source.read_text()
    else:
        assert not source.read_text()


async def test_reindex_refreshes_read_only_without_embedding(bm25_only_components, bounded_config):
    comp, directory = bm25_only_components
    source = directory / "line.md"
    source.write_text("word " * 100)
    config = bounded_config.model_copy(update={"memory_dirs": [directory]})
    comp.index_engine = IndexEngine(comp.storage, comp.embedder, config)
    await comp.index_engine.index_file(source)
    chunks = await comp.storage.list_chunks_by_source(source, limit=None)
    for chunk in chunks:
        chunk.metadata = replace(chunk.metadata, source_read_only=False, source_span_hash="old")
    await comp.storage.upsert_chunks(chunks)
    stats = await comp.index_engine.index_file(source)
    assert stats.indexed_chunks == 0 and stats.mutated
    refreshed = await comp.storage.list_chunks_by_source(source, limit=None)
    assert {c.id for c in refreshed} == {c.id for c in chunks}
    assert all(
        c.metadata.source_read_only and c.metadata.source_span_hash is None for c in refreshed
    )
    assert not (await comp.index_engine.index_file(source)).mutated


@pytest.mark.parametrize(
    "field", ["heading_hierarchy", "retrieval_context", "content", "source_file", "tags"]
)
async def test_foreign_bundle_scans_each_field_before_embedding(storage, tmp_path, field):
    good = {"content": "clean body", "source_file": "clean.md", "retrieval_context": "neutral"}
    bad = {**good, "content": "other body"}
    value = "password: synthetic-review-value"
    bad[field] = [value] if field in {"heading_hierarchy", "tags"} else value
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"chunks": [good, bad]}))
    embedder = AsyncMock()
    before = await storage.get_stats()
    with pytest.raises(ImportPrivacyError):
        await import_chunks(storage, embedder, bundle, extract_entities=False)
    embedder.embed_texts.assert_not_awaited()
    assert await storage.get_stats() == before


@pytest.mark.parametrize(
    "assignment",
    [
        'API_KEY="""synthetic-full-secret"""',
        'password = "" "synthetic-full-secret"',
    ],
)
async def test_shipped_masking_policy_blocks_before_embedding(bm25_only_components, assignment):
    from memtomem.indexing.engine import PrivacyRejection

    comp, directory = bm25_only_components
    source = directory / "secret.md"
    source.write_text("```python\n" + assignment + "\n```\n")
    embedder = AsyncMock()
    embedder.dimension = 1024
    comp.index_engine._embedder = embedder
    with pytest.raises(PrivacyRejection):
        await comp.index_engine.index_file(source)
    embedder.embed_texts.assert_not_awaited()
    assert not await comp.storage.list_chunks_by_source(source)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
async def test_frontmatter_newline_parity(bm25_only_components, newline):
    comp, directory = bm25_only_components
    source = directory / "dated.md"

    def write_date(date):
        source.write_bytes(
            (
                "---\nvalid_to: " + date + "\ntags: [dated]\n"
                "redaction: documents-patterns\n---\n\n# Title\n\npassword: example\n"
            )
            .replace("\n", newline)
            .encode()
        )

    write_date("2020-01-01")
    original = source.read_bytes()
    stats = await comp.index_engine.index_file(source)
    assert stats.exempted_files == 1
    chunks = await comp.storage.list_chunks_by_source(source)
    assert chunks and all(c.metadata.valid_to_unix == 1577923199 for c in chunks)
    assert all("dated" in c.metadata.tags for c in chunks)
    assert source.read_bytes() == original
    preview = comp.index_engine.chunk_content(source, source.read_text(), exempt=True)
    assert sorted(c.content_hash for c in preview) == sorted(c.content_hash for c in chunks)
    write_date("2040-01-01")
    await comp.index_engine.index_file(source)
    refreshed = await comp.storage.list_chunks_by_source(source)
    assert all(c.metadata.valid_to_unix != 1577923199 for c in refreshed)
    assert all(c.metadata.valid_to_unix is not None for c in refreshed)


async def test_self_export_preserves_read_only_without_granting_source_evidence(storage, tmp_path):
    chunk = Chunk(
        content="safe body",
        metadata=ChunkMetadata(
            source_file=tmp_path / "example.md",
            heading_hierarchy=("password: synthetic-review-value",),
            retrieval_context="neutral",
            source_read_only=True,
            source_span_hash="untrusted",
        ),
    )
    await storage.upsert_chunks([chunk])
    path = tmp_path / "self.json"
    await export_chunks(storage, output_path=path)
    await storage.delete_by_source(chunk.metadata.source_file)
    embedder = AsyncMock()
    embedder.embed_texts.side_effect = lambda texts: [None for _ in texts]
    result = await import_chunks(storage, embedder, path, extract_entities=False)
    assert result.imported_chunks == 1
    (restored,) = await storage.list_chunks_by_source(chunk.metadata.source_file)
    assert restored.metadata.source_read_only
    assert restored.metadata.source_span_hash is None
    assert restored.metadata.heading_hierarchy == chunk.metadata.heading_hierarchy
    record = _chunk_to_dict(restored)
    record.update(source_span_hash="forged", source_read_only=False)
    foreign, _ = _dict_to_chunk(record)
    assert foreign.metadata.source_span_hash is None


@pytest.mark.parametrize("history", ["fresh", "main", "pr", "both"])
async def test_schema_histories_converge_and_reopen(tmp_path, history):
    path = tmp_path / "history.db"
    config = StorageConfig(sqlite_path=path)
    store = SqliteBackend(config, dimension=2)
    await store.initialize()
    await store.close()
    # Reconstruct each released branch's append order without relying on the
    # current create_tables implementation for that order.
    with sqlite3.connect(path) as db:
        for column in (
            "source_span_hash",
            "retrieval_context",
            "redaction_count",
            "source_read_only",
        ):
            db.execute(f"ALTER TABLE chunks DROP COLUMN {column}")  # noqa: S608
        if history in {"main", "both"}:
            db.execute("ALTER TABLE chunks ADD COLUMN source_span_hash TEXT")
        if history in {"pr", "both"}:
            db.execute("ALTER TABLE chunks ADD COLUMN retrieval_context TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE chunks ADD COLUMN redaction_count INTEGER NOT NULL DEFAULT 0")
    chunk = Chunk(
        content="schema body",
        embedding=[0.1, 0.2],
        metadata=ChunkMetadata(
            source_file=tmp_path / "note.md",
            source_span_hash="a" * 64,
            retrieval_context="contextuniqueterm",
            redaction_count=3,
            source_read_only=True,
        ),
    )
    store = SqliteBackend(config, dimension=2)
    await store.initialize()
    try:
        await store.upsert_chunks([chunk])
    finally:
        await store.close()
    store = SqliteBackend(config, dimension=2)
    await store.initialize()
    try:
        saved = await store.get_chunk(chunk.id)
        assert saved is not None and saved.metadata == chunk.metadata
        await store.rebuild_fts()
        (hit,) = await store.bm25_search("contextuniqueterm", 10)
        assert hit.chunk.metadata == chunk.metadata
        assert _dict_to_chunk(_chunk_to_dict(saved))[0].metadata.source_read_only
        assert store._get_db().execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        await store.close()
