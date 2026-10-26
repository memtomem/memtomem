"""Regressions from the independent whole-commit format review."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx

from helpers import set_home
from memtomem import provenance
from memtomem._frontmatter import flat_yaml, yaml_list
from memtomem.chunking.markdown import MarkdownChunker
from memtomem.config import IndexingConfig
from memtomem.models import Chunk, ChunkMetadata
from memtomem.tools.export_import import (
    ExportBundle,
    _chunk_to_dict,
    _dict_to_chunk,
    export_chunks,
    import_chunks,
)
from test_import_provenance_gate import _FakeEmbedder, _make_storage, _record, _web_app


@pytest.fixture
async def storage(tmp_path):
    store = await _make_storage(tmp_path)
    yield store
    await store.close()


def legacy_file(tmp_path, record, version="3", *, signed=False):
    data = {"version": version, "chunks": [record], "total_chunks": 1}
    if signed:
        key = provenance.load_or_create_key_for_export(tmp_path / "key")
        data["provenance"] = provenance.make_marker([record], key)
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("version", ["1", "2", "3"])
@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.asyncio
async def test_canonical_user_legacy_restores(storage, tmp_path, monkeypatch, version, signed):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    source = home / ".memtomem/memories/n.md"
    path = legacy_file(tmp_path, _record("ordinary", source=str(source)), version, signed=signed)
    stats = await import_chunks(
        storage,
        _FakeEmbedder(),
        path,
        indexing_config=IndexingConfig(),
        provenance_key_path=tmp_path / "key",
    )
    assert stats.imported_chunks == 1
    row = (await storage.recall_chunks())[0]
    assert row.metadata.scope == "user" and row.metadata.project_root is None
    assert row.metadata.source_file == source
    assert row.metadata.source_span_hash is None


@pytest.mark.parametrize("version", ["1", "2", "3"])
@pytest.mark.parametrize("tilde", [False, True])
@pytest.mark.asyncio
async def test_configured_user_root_can_have_project_spelling(
    storage, tmp_path, monkeypatch, version, tilde
):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    user_root = home / "custom/.memtomem/memories"
    configured = Path("~/custom/.memtomem/memories") if tilde else user_root
    path = legacy_file(tmp_path, _record("ordinary", source=str(user_root / "n.md")), version)
    stats = await import_chunks(
        storage, _FakeEmbedder(), path, indexing_config=IndexingConfig(memory_dirs=[configured])
    )
    assert stats.imported_chunks == 1


@pytest.mark.parametrize("canonical_home", [True, False])
@pytest.mark.asyncio
async def test_registered_project_wins_over_user_roots(
    storage, tmp_path, monkeypatch, canonical_home
):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    root = home if canonical_home else home / "project"
    project_dir = root / ".memtomem/memories"
    path = legacy_file(tmp_path, _record("ordinary", source=str(project_dir / "n.md")), signed=True)
    embedder = _FakeEmbedder()
    embedder.embed_texts = AsyncMock()
    with pytest.raises(ValueError, match="legacy project source"):
        await import_chunks(
            storage,
            embedder,
            path,
            provenance_key_path=tmp_path / "key",
            indexing_config=IndexingConfig(
                memory_dirs=[Path("~")], project_memory_dirs=[project_dir]
            ),
        )
    embedder.embed_texts.assert_not_called()


@pytest.mark.asyncio
async def test_known_user_symlink_escape_stays_rejected(storage, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    user_dir = home / ".memtomem/memories"
    user_dir.mkdir(parents=True)
    target = tmp_path / "foreign/.memtomem/memories/n.md"
    target.parent.mkdir(parents=True)
    target.write_text("ordinary")
    alias = user_dir / "alias.md"
    alias.symlink_to(target)
    path = legacy_file(tmp_path, _record("ordinary", source=str(alias)))
    with pytest.raises(ValueError, match="legacy project source"):
        await import_chunks(storage, _FakeEmbedder(), path, indexing_config=IndexingConfig())


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
@pytest.mark.asyncio
async def test_utc_overflow_rejected_before_embedding(storage, tmp_path, field, value):
    record = _record("ordinary", source=str(tmp_path / "n.md"))
    record[field] = value
    path = legacy_file(tmp_path, record)
    embedder = _FakeEmbedder()
    embedder.embed_texts = AsyncMock()
    with pytest.raises(ValueError, match=field):
        await import_chunks(storage, embedder, path)
    embedder.embed_texts.assert_not_called()
    assert (await storage.get_stats())["total_chunks"] == 0
    with pytest.raises(ValueError, match=field):
        _dict_to_chunk(record)


@pytest.mark.parametrize(
    "value",
    [
        "0001-01-01T00:00:00+00:00",
        "9999-12-31T23:59:59+00:00",
        "0001-01-01T00:00:00-01:00",
        "9999-12-31T23:59:59+01:00",
    ],
)
@pytest.mark.asyncio
async def test_valid_utc_boundaries_still_import(storage, tmp_path, value):
    record = _record("ordinary", source=str(tmp_path / "n.md"))
    record["created_at"] = record["updated_at"] = value
    stats = await import_chunks(storage, _FakeEmbedder(), legacy_file(tmp_path, record))
    assert stats.imported_chunks == 1
    row = (await storage.recall_chunks())[0]
    assert row.created_at == datetime.fromisoformat(value).astimezone(timezone.utc)
    assert row.updated_at == row.created_at


@pytest.mark.asyncio
async def test_self_relations_omitted_and_reported_without_losing_valid_data(
    storage, tmp_path, caplog
):
    a = Chunk("first", ChunkMetadata(source_file=tmp_path / "a.md"))
    b = Chunk("second", ChunkMetadata(source_file=tmp_path / "b.md"))
    await storage.upsert_chunks([a, b])
    await storage.add_relation(a.id, a.id, "related")
    await storage.add_relation(a.id, b.id, "related")
    path = tmp_path / "export.json"
    bundle = await export_chunks(storage, output_path=path)
    assert bundle.total_chunks == 2 and len(bundle.relations) == 1
    assert bundle.omitted_self_relations == 1
    assert "1 self-relation(s)" in caplog.text
    assert ExportBundle.from_json(path.read_text()).omitted_self_relations == 1
    # The source graph itself is not altered by export.
    assert (a.id, "related") in await storage.get_related(a.id)
    await storage.delete_chunks([a.id, b.id])
    stats = await import_chunks(storage, _FakeEmbedder(), path, preserve_ids=True)
    assert stats.imported_chunks == 2 and stats.restored_relations == 1


@pytest.mark.asyncio
async def test_mcp_export_reports_omitted_self_edges(storage, tmp_path, monkeypatch):
    from memtomem.server.tools import export_import as tools

    c = Chunk("ordinary", ChunkMetadata(source_file=tmp_path / "n.md"))
    await storage.upsert_chunks([c])
    await storage.add_relation(c.id, c.id)
    monkeypatch.setattr(
        tools, "_get_app_initialized", AsyncMock(return_value=SimpleNamespace(storage=storage))
    )
    result = await tools.mem_export(output_file=str(tmp_path / "export.json"))
    assert "1 self-relation(s) omitted" in result


@pytest.mark.skipif(os.name == "nt", reason="POSIX path guidance")
@pytest.mark.parametrize("version", ["1", "2", "3", "4"])
@pytest.mark.asyncio
async def test_windows_user_path_error_offers_conversion(storage, tmp_path, version):
    source = r"C:\Users\Example\notes.md"
    if version == "4":
        path = tmp_path / "v4.json"
        path.write_text(
            ExportBundle(
                total_chunks=1,
                chunks=[_chunk_to_dict(Chunk("ordinary", ChunkMetadata(source_file=Path(source))))],
            ).to_json()
        )
    else:
        path = legacy_file(tmp_path, _record("ordinary", source=source), version)
    with pytest.raises(ValueError, match="manually convert source_file.*local absolute path"):
        await import_chunks(storage, _FakeEmbedder(), path)


@pytest.mark.parametrize(
    "items",
    [
        ["issue #12", "rust"],
        ["rust", "issue #12"],
        ["#python", "rust", "#go"],
        ["rust", "literal #12", "go"],
    ],
)
def test_quoted_hash_siblings_do_not_warn(items, caplog):
    assert flat_yaml("tags: " + yaml_list(items)) == {"tags": items}
    assert not caplog.records


@pytest.mark.parametrize(
    "text",
    [
        "description: issue #12\n",
        "tags:\n  - #python\n  - rust\n",
        "tags: [rust # actual comment\n]\n",
    ],
)
def test_true_plain_scalar_comments_still_warn(text, caplog):
    flat_yaml(text)
    assert "quote literal hash characters" in caplog.text


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize(
    "text", ["a: b\n# standalone comment\nc: d\n", "a: b\n# draft: true\n", "a: b\n\n#c\n"]
)
def test_own_line_yaml_comments_do_not_warn(newline, text, caplog):
    result = flat_yaml(text.replace("\n", newline))
    assert result["a"] == "b"
    assert not caplog.records


@pytest.mark.parametrize("version", ["1", "2", "3"])
@pytest.mark.asyncio
async def test_raw_project_spelling_cannot_normalize_into_ordinary_user_path(
    storage, tmp_path, monkeypatch, version
):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    source = tmp_path / "outside/.memtomem/memories/../../notes.md"
    assert ".memtomem" not in source.resolve().parts
    path = legacy_file(tmp_path, _record("ordinary", source=str(source)), version)
    embedder = _FakeEmbedder()
    embedder.embed_texts = AsyncMock()
    with pytest.raises(ValueError, match="legacy project source"):
        await import_chunks(storage, embedder, path, indexing_config=IndexingConfig())
    embedder.embed_texts.assert_not_called()


@pytest.mark.parametrize("linebreak", ["\n", "\r\n", "\r", "\x85", "\u2028", "\u2029"])
def test_trailing_linebreaks_survive_yaml_and_agent_roundtrip(linebreak):
    import yaml

    from memtomem._frontmatter import yaml_scalar
    from memtomem.context.agents import _parse_canonical_agent_text, _subagent_to_claude_md

    value = "hello" + linebreak
    field = "description: " + yaml_scalar(value) + "\n"
    sequence = "tags: " + yaml_list([value, "ordinary"]) + "\n"
    assert yaml.safe_load(field) == {"description": value}
    assert flat_yaml(field) == {"description": value}
    assert yaml.safe_load(sequence) == {"tags": [value, "ordinary"]}
    assert flat_yaml(sequence) == {"tags": [value, "ordinary"]}

    source = "---\nname: demo\n" + field + "---\n\nBody\n"
    agent = _parse_canonical_agent_text(source, source=Path("demo.md"))
    assert agent.description == value
    rendered, _ = _subagent_to_claude_md(agent)
    restored = _parse_canonical_agent_text(rendered, source=Path("demo.md"))
    assert restored.description == value


@pytest.mark.parametrize("version", ["1", "2", "3"])
@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.asyncio
async def test_legacy_self_edges_skip_without_losing_signature_or_valid_edge(
    storage, tmp_path, version, signed
):
    # A signed secret proves the raw self-edge remains in the verified HMAC.
    body = "api_key = sk-" + "x" * 40 if signed else "ordinary"
    records = [
        _record(body, source=str(tmp_path / "a.md")),
        _record("second", source=str(tmp_path / "b.md")),
    ]
    relations = [
        {
            "source_id": records[0]["chunk_id"],
            "target_id": r["chunk_id"],
            "relation_type": "related",
        }
        for r in records
    ]
    payload = {"version": version, "chunks": records, "relations": relations}
    if signed:
        key = provenance.load_or_create_key_for_export(tmp_path / "key")
        payload["provenance"] = provenance.make_marker(records, key, relations)
    path = tmp_path / "legacy-relations.json"
    path.write_text(json.dumps(payload))
    assert ExportBundle.from_json(path.read_text()).relations == relations
    stats = await import_chunks(
        storage, _FakeEmbedder(), path, preserve_ids=True, provenance_key_path=tmp_path / "key"
    )
    assert (stats.imported_chunks, stats.restored_relations, stats.skipped_relations) == (2, 1, 1)
    rows = await storage.recall_chunks()
    first = next(c for c in rows if c.content == body)
    second = next(c for c in rows if c.content == "second")
    assert await storage.get_related(first.id) == [(second.id, "related")]


@pytest.mark.asyncio
async def test_v4_self_edge_still_rejects_before_embedding(storage, tmp_path):
    record = _chunk_to_dict(Chunk("ordinary", ChunkMetadata(source_file=tmp_path / "n.md")))
    path = tmp_path / "v4-self.json"
    payload = json.loads(ExportBundle(total_chunks=1, chunks=[record]).to_json())
    payload["relations"] = [
        {
            "source_id": record["chunk_id"],
            "target_id": record["chunk_id"],
            "relation_type": "related",
        }
    ]
    path.write_text(json.dumps(payload))
    embedder = _FakeEmbedder()
    embedder.embed_texts = AsyncMock()
    with pytest.raises(ValueError, match="self relation"):
        await import_chunks(storage, embedder, path)
    embedder.embed_texts.assert_not_called()
    assert (await storage.get_stats())["total_chunks"] == 0


def overlap_config():
    return IndexingConfig(
        max_chunk_tokens=128,
        target_chunk_tokens=64,
        min_chunk_tokens=8,
        chunk_overlap_tokens=50,
        paragraph_split_threshold=10,
    )


@pytest.mark.asyncio
async def test_raw_chunker_store_export_restore_uses_actual_overlap(storage, tmp_path):
    text = "## Title\n\n" + "s" * 161 + "\n\n" + ("long paragraph words " * 50)
    text += "\n\n" + ("another paragraph words " * 50)
    chunks = MarkdownChunker(overlap_config()).chunk_file(tmp_path / "n.md", text)
    assert len(chunks) > 1
    assert chunks[0].metadata.overlap_before == 0
    assert chunks[1].metadata.overlap_before == 161
    for chunk in chunks:
        assert 0 <= chunk.metadata.overlap_before <= len(chunk.content)
        assert chunk.metadata.overlap_after == 0
    await storage.upsert_chunks(chunks)
    path = tmp_path / "raw.json"
    bundle = await export_chunks(storage, output_path=path)
    assert sorted(r["content"] for r in bundle.chunks) == sorted(c.content for c in chunks)
    await storage.delete_chunks([c.id for c in chunks])
    stats = await import_chunks(storage, _FakeEmbedder(), path, preserve_ids=True)
    assert stats.imported_chunks == len(chunks)
    restored = await storage.get_chunks_batch([c.id for c in chunks])
    for chunk in chunks:
        assert restored[chunk.id].content == chunk.content
        assert restored[chunk.id].metadata.overlap_before == chunk.metadata.overlap_before


def test_raw_overlap_excludes_stripped_borrowed_whitespace(tmp_path):
    text = "## Title\n\n" + "A" * 400 + " " * 199 + "Z\n\n" + "B" * 600
    chunks = MarkdownChunker(overlap_config()).chunk_file(tmp_path / "n.md", text)
    assert len(chunks) == 2
    assert chunks[1].content == "Z\n\n" + "B" * 600
    assert chunks[1].metadata.overlap_before == 1
    assert chunks[1].line_map[0] < 0


@pytest.mark.parametrize("single_merged", [False, True])
@pytest.mark.asyncio
async def test_normal_indexer_overlap_exports_without_repair(
    storage, tmp_path, caplog, single_merged
):
    from memtomem.indexing.engine import IndexEngine

    source = tmp_path / "indexed.md"
    first = "s" * (16 if single_merged else 161)
    text = "## Title\n\n" + first + "\n\n" + "B" * 600
    if not single_merged:
        text += "\n\n" + "C" * 600
    source.write_text(text)
    config = overlap_config()
    config.memory_dirs = [tmp_path]
    engine = IndexEngine(storage, _FakeEmbedder(), config)
    stats = await engine.index_file(source)
    assert not stats.errors
    chunks = await storage.list_chunks_by_source(source)
    assert len(chunks) == 1 if single_merged else len(chunks) > 1
    for chunk in chunks:
        assert 0 <= chunk.metadata.overlap_before <= len(chunk.content)
        assert 0 <= chunk.metadata.overlap_after <= len(chunk.content)
    bundle = await export_chunks(storage)
    assert bundle.total_chunks == len(chunks)
    assert "bounded stale overlap" not in caplog.text


@pytest.mark.parametrize("before,after", [(999, 999), (-5, 999), (999, -5)])
@pytest.mark.asyncio
async def test_stale_overlap_repaired_only_in_export_copy(storage, tmp_path, caplog, before, after):
    chunk = Chunk(
        "ordinary",
        ChunkMetadata(source_file=tmp_path / "n.md", overlap_before=before, overlap_after=after),
    )
    await storage.upsert_chunks([chunk])
    path = tmp_path / "bounded.json"
    bundle = await export_chunks(storage, output_path=path)
    record = ExportBundle.from_json(path.read_text()).chunks[0]
    assert record["overlap_before"] == min(max(before, 0), len(chunk.content))
    assert record["overlap_after"] == min(max(after, 0), len(chunk.content))
    assert record["content"] == chunk.content
    assert "bounded stale overlap metadata for 1 chunk(s)" in caplog.text
    stored = await storage.get_chunk(chunk.id)
    assert stored.metadata.overlap_before == before
    assert stored.metadata.overlap_after == after
    assert chunk.metadata.overlap_before == before
    assert chunk.metadata.overlap_after == after
    await storage.delete_chunks([chunk.id])
    stats = await import_chunks(storage, _FakeEmbedder(), path, preserve_ids=True)
    assert stats.imported_chunks == bundle.total_chunks == 1
    restored = await storage.get_chunk(chunk.id)
    assert restored.content == chunk.content
    assert restored.metadata.overlap_before == record["overlap_before"]
    assert restored.metadata.overlap_after == record["overlap_after"]


@pytest.mark.asyncio
async def test_export_repair_does_not_weaken_import_carry_validation(storage, tmp_path):
    chunk = Chunk("same", ChunkMetadata(source_file=tmp_path / "n.md", overlap_before=999))
    await storage.upsert_chunks([chunk])
    path = legacy_file(tmp_path, _record("same", source=str(chunk.metadata.source_file)))
    embedder = _FakeEmbedder()
    embedder.embed_texts = AsyncMock()
    with pytest.raises(ValueError, match="overlap_before"):
        await import_chunks(storage, embedder, path, on_conflict="update")
    embedder.embed_texts.assert_not_called()
    assert (await storage.get_chunk(chunk.id)).metadata.overlap_before == 999


@pytest.mark.asyncio
async def test_indexed_empty_validity_interval_roundtrips_without_blocking_other_notes(
    storage, tmp_path
):
    from memtomem.indexing.engine import IndexEngine
    from memtomem.models import SearchResult
    from memtomem.search.pipeline import _apply_validity_filter

    engine = IndexEngine(storage, _FakeEmbedder(), IndexingConfig(memory_dirs=[tmp_path]))
    for name, text in [
        ("empty.md", "---\nvalid_from: 2027-01-01\nvalid_to: 2026-01-01\n---\n# T\n\nbody"),
        ("ordinary.md", "# Other\n\nordinary note"),
    ]:
        source = tmp_path / name
        source.write_text(text)
        assert not (await engine.index_file(source)).errors
    chunks = await storage.recall_chunks()
    assert len(chunks) == 2
    empty = next(c for c in chunks if c.metadata.valid_from_unix is not None)
    low, high = empty.metadata.valid_from_unix, empty.metadata.valid_to_unix
    assert low > high
    path = tmp_path / "empty-window.json"
    bundle = await export_chunks(storage, output_path=path)
    assert bundle.total_chunks == 2
    await storage.delete_chunks([c.id for c in chunks])
    stats = await import_chunks(storage, _FakeEmbedder(), path, preserve_ids=True)
    assert stats.imported_chunks == 2
    restored = await storage.get_chunk(empty.id)
    assert (restored.metadata.valid_from_unix, restored.metadata.valid_to_unix) == (low, high)
    result = SearchResult(chunk=restored, score=1.0, rank=1, source="fused")
    for as_of in (high - 1, high, low, low + 1):
        assert _apply_validity_filter([result], as_of) == []


@pytest.mark.parametrize("simulate_windows_decode", [False, True])
@pytest.mark.asyncio
async def test_posix_project_mapping_uses_original_wire_paths(
    storage, tmp_path, monkeypatch, simulate_windows_decode
):
    from memtomem.tools import export_import

    destination = tmp_path / "destination"
    memory_dir = destination / ".memtomem/memories.local"
    memory_dir.mkdir(parents=True)
    record = _chunk_to_dict(Chunk("mapped body", ChunkMetadata(source_file=tmp_path / "n.md")))
    record.update(
        scope="project_local",
        project_root="/home/u/project",
        source_file="/home/u/project/.memtomem/memories.local/n.md",
    )
    path = tmp_path / "posix-project.json"
    path.write_text(ExportBundle(chunks=[record], total_chunks=1).to_json())
    if simulate_windows_decode:
        decode = export_import._dict_to_chunk

        def windows_decode(*args, **kwargs):
            chunk, bid = decode(*args, **kwargs)
            chunk.metadata = replace(
                chunk.metadata,
                source_file=PureWindowsPath(record["source_file"]),
                project_root=PureWindowsPath(record["project_root"]),
            )
            return chunk, bid

        monkeypatch.setattr(export_import, "_dict_to_chunk", windows_decode)
    stats = await import_chunks(
        storage,
        _FakeEmbedder(),
        path,
        indexing_config=IndexingConfig(project_memory_dirs=[memory_dir]),
        project_root_map={record["project_root"]: str(destination)},
    )
    assert stats.imported_chunks == 1
    row = (await storage.recall_chunks(project_context_root=destination))[0]
    assert row.metadata.source_file == memory_dir / "n.md"
    assert row.metadata.project_root == destination
    assert row.metadata.scope == "project_local"
    assert row.metadata.source_span_hash is None


@pytest.mark.parametrize("version", ["1", "2", "3", "4"])
@pytest.mark.asyncio
async def test_windows_rejects_foreign_posix_user_path_before_embedding(
    storage, tmp_path, monkeypatch, version
):
    from memtomem.tools import bundle_format

    source = "/home/u/notes.md"
    if version == "4":
        record = _chunk_to_dict(Chunk("ordinary", ChunkMetadata(source_file=tmp_path / "n.md")))
        record["source_file"] = source
        path = tmp_path / "foreign-posix.json"
        path.write_text(ExportBundle(chunks=[record], total_chunks=1).to_json())
    else:
        path = legacy_file(tmp_path, _record("ordinary", source=source), version)
    # Patch this module's platform view without replacing global os.name/Path.
    monkeypatch.setattr(bundle_format, "os", SimpleNamespace(name="nt"))
    embedder = _FakeEmbedder()
    embedder.embed_texts = AsyncMock()
    with pytest.raises(ValueError, match="on Windows, manually convert source_file"):
        await import_chunks(storage, embedder, path)
    embedder.embed_texts.assert_not_called()
    assert (await storage.get_stats())["total_chunks"] == 0


@pytest.mark.skipif(os.name == "nt", reason="simulate Windows with an existing POSIX root")
@pytest.mark.asyncio
async def test_windows_needs_explicit_map_even_if_foreign_project_path_exists(
    storage, tmp_path, monkeypatch
):
    from memtomem.tools import bundle_format

    memory_dir = tmp_path / ".memtomem/memories.local"
    memory_dir.mkdir(parents=True)
    record = _chunk_to_dict(
        Chunk(
            "ordinary",
            ChunkMetadata(
                source_file=memory_dir / "n.md", project_root=tmp_path, scope="project_local"
            ),
        )
    )
    path = tmp_path / "foreign-existing.json"
    path.write_text(ExportBundle(chunks=[record], total_chunks=1).to_json())
    monkeypatch.setattr(bundle_format, "os", SimpleNamespace(name="nt"))
    embedder = _FakeEmbedder()
    embedder.embed_texts = AsyncMock()
    with pytest.raises(ValueError, match="project_root_map required"):
        await import_chunks(
            storage,
            embedder,
            path,
            indexing_config=IndexingConfig(project_memory_dirs=[memory_dir]),
        )
    embedder.embed_texts.assert_not_called()


@pytest.mark.parametrize("linebreak", ["\x85", "\u2028", "\u2029"])
def test_writer_json_tags_keep_unicode_separators(tmp_path, linebreak):
    from memtomem.tools.memory_writer import format_entry_block

    tags = ["a" + linebreak + "b", "한글", "x,y", 'a"b']
    value = json.dumps(tags, ensure_ascii=False)
    assert MarkdownChunker._parse_tags_value(value, []) == tags
    text = format_entry_block("ordinary body", title="Title", tags=tags)
    chunks = MarkdownChunker().chunk_file(tmp_path / "note.md", text)
    assert len(chunks) == 1
    assert set(chunks[0].metadata.tags) == set(tags)
    assert chunks[0].content == "ordinary body"


@pytest.mark.asyncio
async def test_web_export_validation_error_is_actionable_without_content(storage, tmp_path):
    chunk = Chunk(
        "private body must stay out of errors",
        ChunkMetadata(source_file=tmp_path / "n.md", start_line=2, end_line=1),
    )
    await storage.upsert_chunks([chunk])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_web_app(storage, _FakeEmbedder())),
        base_url="http://test",
    ) as client:
        response = await client.get("/export")
    assert response.status_code == 422
    assert str(chunk.id) in response.json()["detail"]
    assert "line range" in response.json()["detail"]
    assert chunk.content not in response.text


@pytest.mark.asyncio
async def test_web_export_does_not_expose_unclassified_value_errors(storage, monkeypatch):
    from memtomem.tools import export_import

    message = "unexpected internal detail must stay private"
    monkeypatch.setattr(export_import, "export_chunks", AsyncMock(side_effect=ValueError(message)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=_web_app(storage, _FakeEmbedder()), raise_app_exceptions=False
        ),
        base_url="http://test",
    ) as client:
        response = await client.get("/export")
    assert response.status_code == 500
    assert message not in response.text
