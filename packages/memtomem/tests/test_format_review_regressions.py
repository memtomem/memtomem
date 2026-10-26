"""Regressions from the independent whole-commit format review."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from helpers import set_home
from memtomem import provenance
from memtomem._frontmatter import flat_yaml, yaml_list
from memtomem.config import IndexingConfig
from memtomem.models import Chunk, ChunkMetadata
from memtomem.tools.export_import import (
    ExportBundle,
    _chunk_to_dict,
    _dict_to_chunk,
    export_chunks,
    import_chunks,
)
from test_import_provenance_gate import _FakeEmbedder, _make_storage, _record


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
