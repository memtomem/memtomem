"""Cross-boundary regressions: preserve meaning, reject before side effects."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from memtomem import provenance
from memtomem.config import EmbeddingConfig, IndexingConfig, Mem2MemConfig
from memtomem.models import Chunk, ChunkMetadata
from memtomem.storage.sqlite_export import read_export
from memtomem.tools.export_import import (
    ExportBundle,
    ImportPrivacyError,
    _chunk_to_dict,
    export_chunks,
    import_chunks,
)
from test_import_provenance_gate import _FakeEmbedder, _make_storage, _record, _web_app


@pytest.fixture
async def storage(tmp_path):
    store = await _make_storage(tmp_path)
    yield store
    await store.close()


def bundle_file(tmp_path, records, **kwargs):
    path = tmp_path / "import.json"
    path.write_text(json.dumps({"version": "3", "chunks": records, **kwargs}), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "body",
    [
        "[]",
        "null",
        '{"version":"999","chunks":[]}',
        '{"format":"memtomem-context-artifact-bundle","version":"1","chunks":[]}',
        '{"chunks":[],"chunks":[]}',
        '{"chunks":"hello"}',
        '{"chunks":[],"total_chunks":true}',
        '{"chunks":[],"total_chunks":1}',
    ],
)
@pytest.mark.asyncio
async def test_bad_envelope_rejects_without_embedding(storage, tmp_path, body):
    path = tmp_path / "bad.json"
    path.write_text(body)
    embedder = _FakeEmbedder()
    embedder.embed_texts = AsyncMock()
    with pytest.raises(ValueError):
        await import_chunks(storage, embedder, path)
    embedder.embed_texts.assert_not_called()
    assert (await storage.get_stats())["total_chunks"] == 0


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("tags", "work"),
        ("tags", [123]),
        ("heading_hierarchy", "Section"),
        ("source_read_only", "false"),
        ("start_line", True),
        ("start_line", -1),
        ("end_line", 0),
        ("scope", "unexpected"),
        ("overlap_before", 999),
        ("valid_from_unix", True),
        ("chunk_id", "not-uuid"),
    ],
)
@pytest.mark.asyncio
async def test_bad_record_rejects_entire_bundle(storage, tmp_path, key, value):
    bad = _record("bad")
    bad[key] = value
    path = bundle_file(tmp_path, [_record("good"), bad])
    with pytest.raises(ValueError):
        await import_chunks(storage, _FakeEmbedder(), path)
    assert (await storage.get_stats())["total_chunks"] == 0


@pytest.mark.asyncio
async def test_v4_project_mapping_preserves_metadata_and_visibility(storage, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    dirs = [new / ".memtomem/memories.local"]
    config = IndexingConfig(project_memory_dirs=dirs)
    created = datetime(2024, 1, 1, tzinfo=timezone.utc)
    updated = datetime(2025, 1, 1, tzinfo=timezone.utc)
    c = Chunk(
        "A scoped note with history",
        ChunkMetadata(
            source_file=old / ".memtomem/memories.local/note.md",
            scope="project_local",
            project_root=old,
            valid_from_unix=1,
            valid_to_unix=2,
            overlap_before=2,
            overlap_after=3,
            source_span_hash="a" * 64,
        ),
        created_at=created,
        updated_at=updated,
    )
    path = tmp_path / "v4.json"
    path.write_text(ExportBundle(chunks=[_chunk_to_dict(c)], total_chunks=1).to_json())
    stats = await import_chunks(
        storage,
        _FakeEmbedder(),
        path,
        indexing_config=config,
        project_root_map={str(old): str(new) + "/"},
        preserve_ids=True,
    )
    out = (await storage.get_chunks_batch([c.id]))[c.id]
    assert out.metadata.scope == "project_local" and out.metadata.project_root == new
    assert out.metadata.source_file == dirs[0] / "note.md"
    assert (out.metadata.valid_from_unix, out.metadata.valid_to_unix) == (1, 2)
    assert (out.metadata.overlap_before, out.metadata.overlap_after) == (2, 3)
    assert out.metadata.source_span_hash is None
    assert out.created_at == created and out.updated_at == updated
    assert await storage.recall_chunks(project_context_root=tmp_path / "other") == []
    assert len(await storage.recall_chunks(project_context_root=new)) == 1
    assert stats.missing_source_files == 1


@pytest.mark.parametrize("version", ["1", "2", "3"])
@pytest.mark.asyncio
async def test_unregistered_legacy_project_never_becomes_user(storage, tmp_path, version):
    path = bundle_file(
        tmp_path, [_record("legacy", source=str(tmp_path / "foreign/.memtomem/memories/a.md"))]
    )
    data = json.loads(path.read_text())
    data["version"] = version
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        await import_chunks(storage, _FakeEmbedder(), path, indexing_config=IndexingConfig())
    assert (await storage.get_stats())["total_chunks"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("signed", [True, False])
async def test_shared_scope_never_bypasses_privacy(storage, tmp_path, signed, monkeypatch):
    from memtomem import privacy
    from unittest.mock import Mock

    confirmation = Mock()
    monkeypatch.setattr(privacy, "emit_project_shared_confirmation", confirmation)
    root = tmp_path / "project"
    root.mkdir()
    config = IndexingConfig(project_memory_dirs=[root / ".memtomem/memories"])
    c = Chunk(
        "api_key=sk-" + "a" * 30,
        ChunkMetadata(
            source_file=root / ".memtomem/memories/n.md", scope="project_shared", project_root=root
        ),
    )
    await storage.upsert_chunks([c])
    path = tmp_path / "shared.json"
    await export_chunks(storage, output_path=path, stamp_provenance=signed)
    await storage.delete_chunks([c.id])
    with pytest.raises(ImportPrivacyError):
        await import_chunks(
            storage,
            _FakeEmbedder(),
            path,
            indexing_config=config,
            confirm_project_shared=True,
            force_unsafe=True,
        )
    assert (await storage.get_stats())["total_chunks"] == 0
    confirmation.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_update_keeps_missing_metadata_and_local_proof(storage, tmp_path):
    c = Chunk(
        "note",
        ChunkMetadata(
            source_file=tmp_path / "note.md",
            start_line=1,
            end_line=1,
            valid_to_unix=123,
            source_span_hash="a" * 64,
            overlap_after=1,
        ),
        updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    await storage.upsert_chunks([c])
    r = _record("note", source=str(c.metadata.source_file))
    r["tags"] = ["new"]
    stats = await import_chunks(
        storage, _FakeEmbedder(), bundle_file(tmp_path, [r]), on_conflict="update"
    )
    out = (await storage.get_chunks_batch([c.id]))[c.id]
    assert stats.updated_chunks == 1 and out.metadata.tags == ("new",)
    assert out.metadata.valid_to_unix == 123 and out.updated_at == c.updated_at
    assert out.metadata.source_span_hash == "a" * 64
    r["start_line"] = r["end_line"] = 2
    await import_chunks(storage, _FakeEmbedder(), bundle_file(tmp_path, [r]), on_conflict="update")
    assert (await storage.get_chunks_batch([c.id]))[c.id].metadata.source_span_hash is None


@pytest.mark.asyncio
async def test_embedding_race_rolls_back_import(storage, tmp_path):
    c = Chunk("old", ChunkMetadata(source_file=tmp_path / "a.md"))
    await storage.upsert_chunks([c])
    path = bundle_file(tmp_path, [_record("old", source=str(c.metadata.source_file))])
    embedder = _FakeEmbedder()

    async def race(texts):
        c.metadata = replace(c.metadata, tags=("concurrent",))
        await storage.upsert_chunks([c])
        return [[0.1] * 8 for _ in texts]

    embedder.embed_texts = race
    with pytest.raises(ValueError, match="changed during preparation"):
        await import_chunks(storage, embedder, path, on_conflict="update")
    assert (await storage.get_chunks_batch([c.id]))[c.id].metadata.tags == ("concurrent",)


@pytest.mark.asyncio
async def test_v4_signature_binds_version(storage, tmp_path):
    await storage.upsert_chunks([Chunk("signed", ChunkMetadata(source_file=tmp_path / "s.md"))])
    bundle = await export_chunks(storage)
    key = provenance.load_key_for_verify(provenance.key_path_for_db(storage.db_path))
    assert provenance.verify_bundle_marker(
        bundle.chunks, bundle.provenance, key, bundle.relations, format=bundle.format, version="4"
    )
    assert not provenance.verify_bundle_marker(
        bundle.chunks, bundle.provenance, key, bundle.relations, format=bundle.format, version="3"
    )


@pytest.mark.asyncio
async def test_export_stats_filters_and_held_parity(storage, tmp_path):
    for i in range(4):
        await storage.upsert_chunks(
            [
                Chunk(
                    f"note {i}",
                    ChunkMetadata(
                        source_file=tmp_path / f"{i}.md",
                        tags=("a,b",) if i % 2 else ("a",),
                        namespace="n" if i < 3 else "other",
                    ),
                )
            ]
        )
    await storage.hold_source(tmp_path / "1.md", "scan_missing")
    for filters in [
        {},
        {"tag_filter": "a,b"},
        {"namespace_filter": "n"},
        {"source_filter": "1.md"},
        {"source_filter": "!"},
        {"since": datetime(2030, 1, 1, tzinfo=timezone.utc)},
    ]:
        bundle = await export_chunks(storage, stamp_provenance=False, **filters)
        assert await read_export(storage, count_only=True, **filters) == (
            bundle.total_chunks,
            bundle.omitted_held_sources,
        )


@pytest.mark.asyncio
async def test_export_atomic_failure_and_size_leave_old_file(storage, tmp_path, monkeypatch):
    from memtomem.context import _atomic
    from memtomem.tools import bundle_format

    await storage.upsert_chunks([Chunk("note", ChunkMetadata(source_file=tmp_path / "n.md"))])
    path = tmp_path / "backup.json"
    path.write_bytes(b"old backup")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(_atomic.os, "replace", fail)
        with pytest.raises(OSError):
            await export_chunks(storage, output_path=path)
    assert path.read_bytes() == b"old backup"
    assert not list(tmp_path.glob(".backup.json.*.tmp"))
    monkeypatch.setattr(bundle_format, "MAX_BUNDLE_BYTES", 10)
    with pytest.raises(ValueError, match="100 MiB"):
        await export_chunks(storage, output_path=path)
    assert path.read_bytes() == b"old backup"


@pytest.mark.asyncio
async def test_mcp_resource_happy_path(storage, tmp_path, monkeypatch):
    from memtomem.server import resources

    c = Chunk("full", ChunkMetadata(source_file=tmp_path / "n.md"))
    monkeypatch.setattr(resources, "_get_app_initialized", AsyncMock(return_value=object()))
    monkeypatch.setattr(resources, "resolve_chunk", AsyncMock(return_value=c))
    out = json.loads(await resources.chunk_resource(str(c.id)))
    assert out["content"] == "full" and out["created_at"] == c.created_at.isoformat()


@pytest.mark.asyncio
async def test_web_invalid_root_and_envelope_are_422(storage):
    app = _web_app(storage, _FakeEmbedder())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for data, body in [
            ({}, b"[]"),
            ({"project_root_map": "[]"}, b'{"chunks":[]}'),
            ({"project_root_map": '{"/a":"/b","/a":"/c"}'}, b'{"chunks":[]}'),
        ]:
            response = await client.post(
                "/export/import", data=data, files={"file": ("x.json", body, "application/json")}
            )
            assert response.status_code == 422


def test_yaml_roundtrip_and_plain_byte_stability():
    from memtomem._frontmatter import flat_yaml, yaml_scalar
    from memtomem.chunking.markdown import MarkdownChunker
    from memtomem.context.agents import _parse_canonical_agent_text, _subagent_to_claude_md

    assert yaml_scalar("ordinary description") == "ordinary description"
    for text in ["Reviews: code", "Review # carefully", 'quoted " value', "한글 😀\nnext"]:
        src = "---\nname: demo\ndescription: " + yaml_scalar(text) + "\n---\n\nBody\n"
        agent = _parse_canonical_agent_text(src, source=Path("demo.md"))
        out, _ = _subagent_to_claude_md(agent)
        assert yaml.safe_load(out.split("---\n")[1])["description"] == text
    assert flat_yaml("tags: ['a,b', 'c']\n") == {"tags": ["a,b", "c"]}
    text = "---\nother:\n  tags: [nested]\ntags: [a, b] # comment\nvalid_to: 2024-12-31 # sunset\n---\nBody"
    assert MarkdownChunker._extract_frontmatter_tags(text) == ["a", "b"]
    assert MarkdownChunker._extract_validity_window(text)[1] == 1735689599


def test_basestore_rejects_metadata_secrets_and_corrupt_overwrite(tmp_path):
    from memtomem.integrations.langgraph_store import MemtomemBaseStore, StoreRecordError

    store = MemtomemBaseStore(
        root=tmp_path / "store", embedding=EmbeddingConfig(provider="none", dimension=0)
    )
    try:
        with pytest.raises(ValueError, match="privacy"):
            store.put(("api_key=sk-" + "a" * 30,), "key", {"clean": True})
        with pytest.raises(ValueError):
            store.put(("n",), "key", {"bad": float("nan")})
        store.put(("n",), "key", {"clean": True})
        path = next(store.root.rglob("*.json"))
        path.write_text("{broken")
        with pytest.raises(StoreRecordError):
            store.get(("n",), "key")
        with pytest.raises(StoreRecordError):
            store.put(("n",), "key", {"new": True})
        assert path.read_text() == "{broken"
        assert store.search(("n",)) == []
        store.delete(("n",), "key")
        assert not path.exists()
    finally:
        store.close()


def test_pinned_description_is_guarded(tmp_path):
    from memtomem.pinned import PinnedContextStore

    config = Mem2MemConfig()
    config.indexing.memory_dirs = [tmp_path / "memories"]
    with pytest.raises(ValueError, match="privacy"):
        PinnedContextStore(config).set("note", "clean", description="api_key=sk-" + "a" * 30)
    assert not list(tmp_path.rglob("*.md"))


@pytest.mark.asyncio
async def test_mcp_export_refuses_leaf_symlink(storage, tmp_path, monkeypatch):
    from memtomem.server.tools import export_import as tools

    original = tmp_path / "original.json"
    original.write_text("private")
    link = tmp_path / "backup.json"
    link.symlink_to(original)
    monkeypatch.setattr(
        tools, "_get_app_initialized", AsyncMock(return_value=SimpleNamespace(storage=storage))
    )
    result = await tools.mem_export(output_file=str(link))
    assert "symlink" in result
    assert original.read_text() == "private" and link.is_symlink()
    alias = tmp_path / "parent-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    await export_chunks(storage, output_path=alias / "regular.json")
    assert json.loads((tmp_path / "regular.json").read_text())["version"] == "4"


@pytest.mark.asyncio
async def test_false_user_scope_through_source_symlink_rejected(storage, tmp_path):
    target = tmp_path / "project/.memtomem/memories/n.md"
    target.parent.mkdir(parents=True)
    target.write_text("clean")
    alias = tmp_path / "alias.md"
    alias.symlink_to(target)
    r = _record("clean", source=str(alias))
    with pytest.raises(ValueError):
        await import_chunks(storage, _FakeEmbedder(), bundle_file(tmp_path, [r]))


@pytest.mark.asyncio
async def test_foreign_windows_project_mapping(storage, tmp_path):
    dest = tmp_path / "project"
    dest.mkdir()
    config = IndexingConfig(project_memory_dirs=[dest / ".memtomem/memories.local"])
    c = Chunk(
        "portable",
        ChunkMetadata(
            source_file=Path(r"C:\old\.memtomem\memories.local\n.md"),
            scope="project_local",
            project_root=Path(r"C:\old"),
        ),
    )
    p = tmp_path / "windows.json"
    p.write_text(ExportBundle(chunks=[_chunk_to_dict(c)], total_chunks=1).to_json())
    s = await import_chunks(
        storage, _FakeEmbedder(), p, indexing_config=config, project_root_map={r"C:\old": str(dest)}
    )
    assert s.imported_chunks == 1
    rows = await storage.recall_chunks(project_context_root=dest)
    assert rows[0].metadata.source_file == dest / ".memtomem/memories.local/n.md"


@pytest.mark.asyncio
async def test_export_snapshot_includes_hold_state(storage, tmp_path, monkeypatch):
    from memtomem.storage import sqlite_export

    c = Chunk("snapshot", ChunkMetadata(source_file=tmp_path / "n.md"))
    await storage.upsert_chunks([c])
    counts = sqlite_export._counts

    def change_after_count(db, where, params):
        result = counts(db, where, params)
        writer = storage._get_db()
        writer.execute(
            "INSERT INTO held_sources VALUES (?, ?, ?, ?)",
            (str(c.metadata.source_file), "now", "now", "scan_missing"),
        )
        writer.commit()
        return result

    monkeypatch.setattr(sqlite_export, "_counts", change_after_count)
    bundle = await export_chunks(storage, stamp_provenance=False)
    assert bundle.total_chunks == 1 and bundle.omitted_held_sources == 0


def test_basestore_scans_and_persists_same_copies(tmp_path, monkeypatch):
    from memtomem import privacy
    from memtomem.integrations.langgraph_store import MemtomemBaseStore

    store = MemtomemBaseStore(
        root=tmp_path / "store", embedding=EmbeddingConfig(provider="none", dimension=0)
    )
    value, selectors, namespace = {"text": "clean"}, ["text"], ["notes"]
    guard = privacy.enforce_write_guard

    def mutate(text, **kwargs):
        value["text"] = "changed"
        selectors.append("other")
        namespace[0] = "changed"
        return guard(text, **kwargs)

    monkeypatch.setattr(privacy, "enforce_write_guard", mutate)
    try:
        store.put(namespace, "k", value, index=selectors)
        out = store.get(("notes",), "k")
        assert out.value == {"text": "clean"}
        record = json.loads(next(store.root.rglob("*.json")).read_text())
        assert record["index"] == ["text"] and record["namespace"] == ["notes"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_100001_small_chunks_export_and_restore(tmp_path):
    """No per-source cap; the complete bundle remains under the byte ceiling."""
    import gc
    import hashlib
    from uuid import UUID
    from memtomem.config import StorageConfig
    from memtomem.embedding.noop import NoopEmbedder
    from memtomem.storage.sqlite_backend import SqliteBackend

    src = SqliteBackend(
        StorageConfig(sqlite_path=tmp_path / "src.db"),
        dimension=0,
        embedding_provider="none",
        embedding_model="none",
    )
    dst = SqliteBackend(
        StorageConfig(sqlite_path=tmp_path / "dst.db"),
        dimension=0,
        embedding_provider="none",
        embedding_model="none",
    )
    await src.initialize()
    await dst.initialize()
    try:
        now = "2026-01-01T00:00:00+00:00"
        digest = hashlib.sha256(b"n").hexdigest()
        db = src._get_db()
        db.executemany(
            "INSERT INTO chunks (id,content,content_hash,source_file,start_line,end_line,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                (str(UUID(int=i)), "n", digest, str(tmp_path / "n.md"), i, i, now, now)
                for i in range(1, 100002)
            ),
        )
        db.commit()
        path = tmp_path / "all.json"
        bundle = await export_chunks(src, output_path=path, stamp_provenance=False)
        assert bundle.total_chunks == 100001
        del bundle
        gc.collect()
        stats = await import_chunks(dst, NoopEmbedder(), path, extract_entities=False)
        assert stats.imported_chunks == 100001
        assert (await dst.get_stats())["total_chunks"] == 100001
    finally:
        await src.close()
        await dst.close()


@pytest.mark.parametrize(
    "body",
    [
        '{"chunks":[],"extra":1e999}',
        '{"chunks":[],"exported_at":123}',
        '{"chunks":[],"provenance":[]}',
    ],
)
def test_nonfinite_or_bad_optional_headers_reject(body):
    with pytest.raises(ValueError):
        ExportBundle.from_json(body)


@pytest.mark.asyncio
async def test_byte_ceiling_counts_utf8_and_accepts_exact_boundary(storage, tmp_path, monkeypatch):
    from memtomem.tools import bundle_format, export_import

    body = json.dumps({"chunks": [_record("한글 😀")]}, ensure_ascii=False)
    size = len(body.encode("utf-8"))
    assert size > len(body)
    monkeypatch.setattr(bundle_format, "MAX_BUNDLE_BYTES", size)
    monkeypatch.setattr(export_import, "MAX_BUNDLE_BYTES", size)
    bundle_format.enforce_size(body)
    path = tmp_path / "size.json"
    path.write_text(body, encoding="utf-8")
    stats = await import_chunks(storage, _FakeEmbedder(), path)
    assert stats.imported_chunks == 1
    path.write_bytes(body.encode("utf-8") + b" ")
    embedder = _FakeEmbedder()
    embedder.embed_texts = AsyncMock()
    with pytest.raises(ValueError, match="too large"):
        await import_chunks(storage, embedder, path)
    embedder.embed_texts.assert_not_called()
    with pytest.raises(ValueError, match="100 MiB"):
        bundle_format.enforce_size(body + " ")


@pytest.mark.asyncio
async def test_legacy_namespace_override_survives_missing_field(storage, tmp_path):
    c = Chunk("same", ChunkMetadata(source_file=tmp_path / "n.md", namespace="old"))
    await storage.upsert_chunks([c])
    r = _record("same", source=str(c.metadata.source_file))
    r.pop("namespace", None)
    await import_chunks(
        storage, _FakeEmbedder(), bundle_file(tmp_path, [r]), namespace="new", on_conflict="update"
    )
    assert (await storage.get_chunks_batch([c.id]))[c.id].metadata.namespace == "new"


@pytest.mark.asyncio
async def test_legacy_carryover_preserves_empty_validity_interval(storage, tmp_path):
    c = Chunk("same", ChunkMetadata(source_file=tmp_path / "n.md", valid_to_unix=1))
    await storage.upsert_chunks([c])
    r = _record("same", source=str(c.metadata.source_file))
    r["valid_from_unix"] = 2
    await import_chunks(storage, _FakeEmbedder(), bundle_file(tmp_path, [r]), on_conflict="update")
    meta = (await storage.get_chunks_batch([c.id]))[c.id].metadata
    assert (meta.valid_from_unix, meta.valid_to_unix) == (2, 1)


@pytest.mark.asyncio
async def test_identical_pending_keys_land_once(storage, tmp_path):
    from uuid import uuid4

    first = _record("same", source=str(tmp_path / "n.md"))
    second = {**first, "chunk_id": str(uuid4())}
    stats = await import_chunks(
        storage, _FakeEmbedder(), bundle_file(tmp_path, [first, second]), on_conflict="duplicate"
    )
    assert stats.imported_chunks == 1 and stats.conflict_skipped_chunks == 1
    assert (await storage.get_stats())["total_chunks"] == 1


def test_yaml_anchored_keys_are_not_rewriteable_names():
    from memtomem._frontmatter import flat_yaml

    assert flat_yaml("&anchor name: risky\nname: plain\n") == {"name": "plain"}


@pytest.mark.parametrize("corruption", ["overflow", "future", "utf8"])
def test_basestore_corrupt_record_is_never_silently_replaced(tmp_path, corruption):
    from memtomem.integrations.langgraph_store import MemtomemBaseStore, StoreRecordError

    store = MemtomemBaseStore(
        root=tmp_path / "store", embedding=EmbeddingConfig(provider="none", dimension=0)
    )
    try:
        store.put(("n",), "k", {"x": 0})
        path = next(store.root.rglob("*.json"))
        body = (
            path.read_text().replace('"x": 0', '"x": 1e999')
            if corruption == "overflow"
            else path.read_text().replace('"schema_version": 1', '"schema_version": 2')
        )
        path.write_bytes(b"\xff" if corruption == "utf8" else body.encode())
        original = path.read_bytes()
        with pytest.raises(StoreRecordError):
            store.get(("n",), "k")
        with pytest.raises(StoreRecordError):
            store.put(("n",), "k", {"x": 1})
        assert path.read_bytes() == original
        assert store.search(("n",)) == []
        store.delete(("n",), "k")
        assert not path.exists()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_explicit_v4_user_under_unregistered_memtomem_roundtrips(storage, tmp_path):
    c = Chunk(
        "ordinary user note", ChunkMetadata(source_file=tmp_path / "proj/.memtomem/memories/n.md")
    )
    await storage.upsert_chunks([c])
    path = tmp_path / "v4-user.json"
    await export_chunks(storage, output_path=path)
    await storage.delete_chunks([c.id])
    stats = await import_chunks(
        storage, _FakeEmbedder(), path, preserve_ids=True, indexing_config=IndexingConfig()
    )
    assert stats.imported_chunks == 1
    assert (await storage.get_chunks_batch([c.id]))[c.id].metadata.scope == "user"
    # Foreign claims cannot override a registered destination boundary.
    await storage.delete_chunks([c.id])
    foreign = json.loads(path.read_text())
    foreign.pop("provenance")
    path.write_text(json.dumps(foreign))
    with pytest.raises(ValueError, match="source_file/scope"):
        await import_chunks(
            storage,
            _FakeEmbedder(),
            path,
            indexing_config=IndexingConfig(project_memory_dirs=[c.metadata.source_file.parent]),
        )


@pytest.mark.asyncio
async def test_nul_in_text_fields_and_relation_roundtrips(storage, tmp_path):
    first = Chunk(
        "text\x00after",
        ChunkMetadata(
            source_file=tmp_path / "a.md", tags=("tag\x00x",), heading_hierarchy=("head\x00x",)
        ),
    )
    second = Chunk("target", ChunkMetadata(source_file=tmp_path / "b.md"))
    await storage.upsert_chunks([first, second])
    await storage.add_relation(first.id, second.id, "rel\x00x")
    path = tmp_path / "nul.json"
    await export_chunks(storage, output_path=path)
    await storage.delete_chunks([first.id, second.id])
    stats = await import_chunks(storage, _FakeEmbedder(), path, preserve_ids=True)
    out = (await storage.get_chunks_batch([first.id]))[first.id]
    assert out.content == first.content and out.metadata.tags == first.metadata.tags
    assert out.metadata.heading_hierarchy == first.metadata.heading_hierarchy
    assert stats.restored_relations == 1


def test_hash_comments_are_explicit_and_quoted_hashes_are_lossless(caplog):
    from memtomem.context.agents import _parse_flat_yaml
    from memtomem.chunking.markdown import MarkdownChunker

    assert (
        _parse_flat_yaml("description: Triage GitHub issue #123 and label it")["description"]
        == "Triage GitHub issue"
    )
    assert "quote literal hash characters" in caplog.text
    caplog.clear()
    assert MarkdownChunker._extract_frontmatter_tags("---\ntags: #python\n---\nBody") == []
    assert "quote literal hash characters" in caplog.text
    caplog.clear()
    assert MarkdownChunker._parse_tags_value("#a #b", []) == []
    assert "quote literal hash characters" in caplog.text
    assert (
        _parse_flat_yaml('description: "Triage issue #123"')["description"] == "Triage issue #123"
    )
    assert MarkdownChunker._parse_tags_value('["#a", "#b"]', []) == ["#a", "#b"]


def test_flow_indicators_and_blockquote_prose():
    from memtomem._frontmatter import flat_yaml, yaml_list
    from memtomem.chunking.markdown import MarkdownChunker

    items = ["-", "?x", ":x", "a:b"]
    assert flat_yaml("tags: " + yaml_list(items))["tags"] == items
    assert MarkdownChunker._parse_tags_value("", ["Some prose line", "more"]) == []
    assert MarkdownChunker._parse_tags_value("", ["- one", "prose", "- two"]) == ["one"]


def test_basestore_scan_skips_unreadable_but_exact_read_raises(tmp_path, monkeypatch):
    from memtomem.integrations.langgraph_store import MemtomemBaseStore

    store = MemtomemBaseStore(
        root=tmp_path / "store", embedding=EmbeddingConfig(provider="none", dimension=0)
    )
    try:
        store.put(("n",), "good", {"x": "good"})
        store.put(("n",), "bad", {"x": "bad"})
        read = store._read_record
        bad = store._path(("n",), "bad")

        def unreadable(path):
            if path == bad:
                raise PermissionError("denied")
            return read(path)

        monkeypatch.setattr(store, "_read_record", unreadable)
        assert [item.key for item in store.search(("n",))] == ["good"]
        assert store.list_namespaces() == [("n",)]
        with pytest.raises(PermissionError):
            store.get(("n",), "bad")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_policy_summary_inherits_project_boundary_for_roundtrip(storage, tmp_path):
    from memtomem.tools.consolidation_engine import apply_consolidation

    source = tmp_path / ".memtomem/memories.local/n.md"
    original = Chunk(
        "project original",
        ChunkMetadata(source_file=source, scope="project_local", project_root=tmp_path),
    )
    await storage.upsert_chunks([original])
    sid = await apply_consolidation(
        storage,
        {"source": str(source), "chunk_ids": [str(original.id)]},
        "project summary",
        extract_entities=False,
    )
    summary = (await storage.get_chunks_batch([sid]))[sid]
    assert summary.metadata.scope == "project_local" and summary.metadata.project_root == tmp_path
    path = tmp_path / "summary.json"
    await export_chunks(storage, output_path=path)
    await storage.delete_chunks([original.id, sid])
    stats = await import_chunks(
        storage,
        _FakeEmbedder(),
        path,
        indexing_config=IndexingConfig(project_memory_dirs=[source.parent]),
        preserve_ids=True,
    )
    assert stats.imported_chunks == 2
    assert (await storage.get_chunks_batch([sid]))[sid].metadata.scope == "project_local"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["legacy_policy_summary", "registered_after_index"])
async def test_self_export_keeps_unchanged_stored_user_scope(storage, tmp_path, kind):
    from memtomem.models import ORIGIN_CONSOLIDATION_POLICY

    mem = tmp_path / "proj/.memtomem/memories"
    source = mem / ("n.md.consolidated.md" if kind == "legacy_policy_summary" else "n.md")
    old = Chunk(
        "old stored scope",
        ChunkMetadata(
            source_file=source,
            origin=ORIGIN_CONSOLIDATION_POLICY if kind == "legacy_policy_summary" else None,
        ),
    )
    ordinary = Chunk("ordinary", ChunkMetadata(source_file=tmp_path / "ordinary.md"))
    await storage.upsert_chunks([old, ordinary])
    path = tmp_path / "old-state.json"
    await export_chunks(storage, output_path=path)
    await storage.delete_chunks([old.id, ordinary.id])
    config = IndexingConfig(project_memory_dirs=[mem])
    stats = await import_chunks(
        storage, _FakeEmbedder(), path, preserve_ids=True, indexing_config=config
    )
    assert stats.imported_chunks == 2
    restored = (await storage.get_chunks_batch([old.id]))[old.id]
    assert restored.metadata.scope == "user" and restored.metadata.source_file == source
    # Tampering loses the local replay authority.
    data = json.loads(path.read_text())
    data["chunks"][0]["content"] += " changed"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="index --force"):
        await import_chunks(storage, _FakeEmbedder(), path, indexing_config=config)


@pytest.mark.asyncio
async def test_signed_user_path_alias_cannot_gain_replay_exception(storage, tmp_path):
    source = tmp_path / "ordinary.md"
    c = Chunk("local", ChunkMetadata(source_file=source))
    await storage.upsert_chunks([c])
    path = tmp_path / "before-link.json"
    await export_chunks(storage, output_path=path)
    await storage.delete_chunks([c.id])
    target = tmp_path / "project/.memtomem/memories/n.md"
    target.parent.mkdir(parents=True)
    target.write_text("other")
    source.symlink_to(target)
    with pytest.raises(ValueError, match="record 0.*source_file/scope"):
        await import_chunks(
            storage,
            _FakeEmbedder(),
            path,
            indexing_config=IndexingConfig(project_memory_dirs=[target.parent]),
        )


@pytest.mark.asyncio
async def test_deregistered_shared_scope_still_requires_registration(storage, tmp_path):
    c = Chunk(
        "shared",
        ChunkMetadata(
            source_file=tmp_path / ".memtomem/memories/n.md",
            scope="project_shared",
            project_root=tmp_path,
        ),
    )
    await storage.upsert_chunks([c])
    path = tmp_path / "deregistered.json"
    await export_chunks(storage, output_path=path)
    await storage.delete_chunks([c.id])
    with pytest.raises(ValueError, match="register the destination"):
        await import_chunks(
            storage,
            _FakeEmbedder(),
            path,
            indexing_config=IndexingConfig(),
            confirm_project_shared=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["start_line", "end_line", "redaction_count", "valid_from_unix", "valid_to_unix"]
)
@pytest.mark.parametrize("value", [10**30, -(10**30)])
async def test_sqlite_integer_overflow_rejects_before_embedding(storage, tmp_path, field, value):
    record = _record("safe")
    record[field] = value
    if field in {"start_line", "end_line"}:
        record["start_line"] = record["end_line"] = value
    embedder = _FakeEmbedder()
    embedder.embed_texts = AsyncMock()
    with pytest.raises(ValueError, match="record 0"):
        await import_chunks(storage, embedder, bundle_file(tmp_path, [record]))
    embedder.embed_texts.assert_not_called()


def test_sequence_comment_and_mapping_items_warn_without_values(caplog):
    from memtomem.chunking.markdown import MarkdownChunker

    assert MarkdownChunker._extract_frontmatter_tags(
        "---\ntags:\n  - #python\n  - rust\n---\nB"
    ) == ["rust"]
    assert "quote literal hash characters" in caplog.text and "python" not in caplog.text
    caplog.clear()
    assert MarkdownChunker._parse_tags_value("", ["- #a", "- b"]) == ["b"]
    assert "quote literal hash characters" in caplog.text
    caplog.clear()
    assert MarkdownChunker._parse_tags_value("", ["- a: b", "- two"]) == []
    assert "quote literal item text" in caplog.text and "a: b" not in caplog.text


@pytest.mark.asyncio
async def test_export_validation_identifies_stored_chunk(storage, tmp_path):
    c = Chunk(
        "bad line range",
        ChunkMetadata(source_file=tmp_path / "n.md", start_line=2, end_line=1),
    )
    await storage.upsert_chunks([c])
    with pytest.raises(ValueError, match=str(c.id)) as exc:
        await export_chunks(storage)
    assert "No bundle exported" in str(exc.value) and "No records imported" not in str(exc.value)


@pytest.mark.asyncio
async def test_changed_private_metadata_counts_one_blocked_record(storage, tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    new = tmp_path / ("api_key=sk-" + "a" * 30)
    new.mkdir()
    c = Chunk(
        "ordinary",
        ChunkMetadata(
            source_file=old / ".memtomem/memories.local/n.md",
            scope="project_local",
            project_root=old,
        ),
    )
    await storage.upsert_chunks([c])
    path = tmp_path / "private.json"
    await export_chunks(storage, output_path=path)
    with pytest.raises(ImportPrivacyError) as exc:
        await import_chunks(
            storage,
            _FakeEmbedder(),
            path,
            project_root_map={str(old): str(new)},
            indexing_config=IndexingConfig(project_memory_dirs=[new / ".memtomem/memories.local"]),
        )
    assert exc.value.blocked_records == 1


@pytest.mark.asyncio
async def test_mapping_error_names_actual_record_index(storage, tmp_path):
    normal = Chunk("ordinary", ChunkMetadata(source_file=tmp_path / "ordinary.md"))
    project = Chunk(
        "project",
        ChunkMetadata(
            source_file=tmp_path / ".memtomem/memories.local/n.md",
            scope="project_local",
            project_root=tmp_path,
        ),
    )
    path = tmp_path / "index.json"
    path.write_text(
        ExportBundle(
            total_chunks=2, chunks=[_chunk_to_dict(normal), _chunk_to_dict(project)]
        ).to_json()
    )
    with pytest.raises(ValueError, match="record 1.*configuration required"):
        await import_chunks(storage, _FakeEmbedder(), path)
