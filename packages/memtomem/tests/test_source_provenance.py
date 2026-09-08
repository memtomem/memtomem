"""#2371: original indexed spans, not inode identity or retrieval text, authorize writes."""

from __future__ import annotations

import dataclasses
import hashlib
import os
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx
from memtomem.chunking.markdown import MarkdownChunker
from memtomem.chunking.registry import ChunkerRegistry
from memtomem.config import IndexingConfig
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.source_provenance import (
    STALE_SOURCE_PROVENANCE_DETAIL,
    StaleSourceProvenanceError,
    source_span_hash,
)
from memtomem.tools import memory_mutation, memory_writer
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured


ORIGINAL = '## Alpha\n\n> tags: ["indexed"]\n\nAlpha body\n'
STRANGER = "## Other\n\nAnother owner\n\nline five\nline six\nline seven\nline eight\n"
SURFACES = ("mcp_edit", "mcp_delete", "web_edit", "web_delete")


@pytest.fixture
async def indexed_source(bm25_only_components):
    comp, directory = bm25_only_components
    source = directory / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")
    stats = await comp.index_engine.index_file(source, already_scanned=True)
    assert not stats.errors
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    assert chunk.metadata.source_span_hash
    return comp, source, chunk


async def _request(comp, surface, chunk_id):
    if surface.startswith("mcp"):
        ctx = StubCtx(AppContext.from_components(comp))
        if surface.endswith("edit"):
            return await memory_crud.mem_edit(str(chunk_id), "NEW BODY", ctx=ctx)
        return await memory_crud.mem_delete(chunk_id=str(chunk_id), ctx=ctx)
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.dependency_overrides[require_configured] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if surface.endswith("edit"):
            return await client.patch(f"/api/chunks/{chunk_id}", json={"new_content": "NEW BODY"})
        return await client.delete(f"/api/chunks/{chunk_id}")


def _assert_stale(result):
    if isinstance(result, str):
        assert result == f"Error: {STALE_SOURCE_PROVENANCE_DETAIL}"
    else:
        assert result.status_code == 409, result.text
        assert result.json()["detail"] == STALE_SOURCE_PROVENANCE_DETAIL


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "change", ["replace", "same_inode", "header_only", "missing", "malformed", "non_utf8"]
)
async def test_stale_span_preserves_source_and_index(indexed_source, monkeypatch, surface, change):
    comp, source, chunk = indexed_source
    before_stat = source.stat()
    if change == "replace":
        replacement = source.with_name("replacement.md")
        replacement.write_text(STRANGER, encoding="utf-8")
        replacement.replace(source)  # BEFORE the span's own read (#2371 witness).
        assert len(STRANGER.splitlines()) > chunk.metadata.end_line
    elif change == "same_inode":
        source.write_text(ORIGINAL.replace("Alpha body", "Other body"), encoding="utf-8")
        os.utime(source, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
        after_stat = source.stat()
        assert (after_stat.st_ino, after_stat.st_size, after_stat.st_mtime_ns) == (
            before_stat.st_ino,
            before_stat.st_size,
            before_stat.st_mtime_ns,
        )
    elif change == "header_only":
        # Retrieval content is unchanged, but DELETE would remove changed metadata.
        source.write_text(ORIGINAL.replace("indexed", "changed"), encoding="utf-8")
    elif change in ("missing", "malformed"):
        chunk.metadata = dataclasses.replace(
            chunk.metadata, source_span_hash=None if change == "missing" else "not-a-digest"
        )
        await comp.storage.upsert_chunks([chunk])
    else:
        source.write_bytes(b"\xff\n" * 8)
    source_before = source.read_bytes()
    rows_before = comp.storage._get_db().execute("SELECT * FROM chunks").fetchall()
    index = AsyncMock(wraps=comp.index_engine.index_file)
    monkeypatch.setattr(comp.index_engine, "index_file", index)
    restores = []
    for module in (memory_crud, memory_mutation):
        restore = Mock(wraps=module.restore_pre_image_quietly)
        monkeypatch.setattr(module, "restore_pre_image_quietly", restore)
        restores.append(restore)
    _assert_stale(await _request(comp, surface, chunk.id))
    assert source.read_bytes() == source_before
    assert comp.storage._get_db().execute("SELECT * FROM chunks").fetchall() == rows_before
    index.assert_not_awaited()
    for restore in restores:
        restore.assert_not_called()


@pytest.mark.parametrize("surface", SURFACES)
async def test_plain_reindex_adopts_legacy_evidence_and_allows_write(indexed_source, surface):
    comp, source, chunk = indexed_source
    chunk.metadata = dataclasses.replace(chunk.metadata, source_span_hash=None)
    await comp.storage.upsert_chunks([chunk])
    before = (
        comp.storage._get_db()
        .execute("SELECT * FROM chunks WHERE id=?", (str(chunk.id),))
        .fetchone()
    )
    _assert_stale(await _request(comp, surface, chunk.id))
    stats = await comp.index_engine.index_file(source, already_scanned=True)
    assert not stats.errors
    assert stats.mutated
    assert stats.indexed_chunks == 0  # No forced re-embedding to backfill evidence.
    fresh = await comp.storage.get_chunk(chunk.id)
    assert fresh is not None and fresh.metadata.source_span_hash
    after = (
        comp.storage._get_db()
        .execute("SELECT * FROM chunks WHERE id=?", (str(chunk.id),))
        .fetchone()
    )
    assert before[:24] == after[:24]  # All existing fields, including UUID/timestamps.
    result = await _request(comp, surface, chunk.id)
    if isinstance(result, str):
        assert result.startswith("Memory "), result
    else:
        assert result.status_code == 200, result.text
    assert (
        ("NEW BODY" in source.read_text()) if surface.endswith("edit") else not source.read_text()
    )


async def test_plain_reindex_refreshes_hash_when_retrieval_content_is_unchanged(indexed_source):
    comp, source, chunk = indexed_source
    # Wikilink aliases have the same indexed text but different source spelling.
    source.write_text(ORIGINAL.replace("Alpha body", "[[target|Alpha body]]"), encoding="utf-8")
    stats = await comp.index_engine.index_file(source, already_scanned=True)
    fresh = await comp.storage.get_chunk(chunk.id)
    assert fresh is not None and fresh.content == chunk.content
    assert fresh.metadata.source_span_hash != chunk.metadata.source_span_hash
    assert stats.mutated and stats.indexed_chunks == 0
    assert fresh.updated_at == chunk.updated_at
    assert not (await comp.index_engine.index_file(source, already_scanned=True)).mutated


@pytest.mark.parametrize("surface", SURFACES)
async def test_changes_outside_target_span_do_not_block_write(indexed_source, surface):
    comp, source, chunk = indexed_source
    with source.open("a", encoding="utf-8") as handle:
        handle.write("\n## Later\n\nExternal append\n")
    result = await _request(comp, surface, chunk.id)
    if isinstance(result, str):
        assert result.startswith("Memory "), result
    else:
        assert result.status_code == 200, result.text
    assert "External append" in source.read_text()


@pytest.mark.parametrize("operation", ["replace_chunk_body", "replace_lines", "remove_lines"])
@pytest.mark.parametrize("identity_known", [True, False])
def test_same_inode_change_after_preimage_is_refused(tmp_path, operation, identity_known):
    source = tmp_path / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")
    digest = source_span_hash(ORIGINAL.splitlines(), 1, 5)
    pre = memory_writer.read_pre_image(source)
    source.write_text(STRANGER, encoding="utf-8")
    args = [source, 1, 5]
    if operation != "remove_lines":
        args.append("NEW BODY")
    with pytest.raises(StaleSourceProvenanceError):
        getattr(memory_writer, operation)(
            *args,
            expected_source_span_hash=digest,
            expected_identity=pre.identity if identity_known else None,
        )
    assert source.read_text() == STRANGER


@pytest.mark.parametrize("newline,final", [("\n", True), ("\r\n", True), ("\n", False)])
@pytest.mark.parametrize("shape", ["metadata", "merged", "overlap", "split"])
@pytest.mark.parametrize("operation", ["replace_chunk_body", "remove_lines"])
async def test_transformed_chunks_keep_original_span_evidence(
    bm25_only_components, newline, final, shape, operation
):
    comp, directory = bm25_only_components
    text = ORIGINAL.replace("Alpha body", "[[target|Alpha body]]")
    config = IndexingConfig(memory_dirs=[directory])
    if shape == "merged":
        text = "# Parent\n\n## Left\n\nleft\n\n## Right\n\nright\n"
    elif shape in ("overlap", "split"):
        config = IndexingConfig(
            memory_dirs=[directory],
            min_chunk_tokens=0,
            max_chunk_tokens=64,
            target_chunk_tokens=48,
            chunk_overlap_tokens=8 if shape == "overlap" else 0,
            paragraph_split_threshold=1,
        )
        text += "\n\n".join(f"Paragraph {i}: " + "body words " * 12 for i in range(10)) + "\n"
    comp.index_engine._config = config
    comp.index_engine._registry = ChunkerRegistry([MarkdownChunker(config)])
    source = directory / "transformed.md"
    text = text if final else text.rstrip("\n")
    original = text.replace("\n", newline).encode()
    source.write_bytes(original)
    stats = await comp.index_engine.index_file(source, already_scanned=True)
    assert not stats.errors
    chunks = await comp.storage.list_chunks_by_source(source)
    assert chunks
    if shape in ("overlap", "split"):
        assert len(chunks) > 1
    if shape == "overlap":
        assert any(c.metadata.overlap_before for c in chunks)
    if shape == "merged":
        assert len(chunks) == 1
        assert "## Left" in chunks[0].content and "## Right" in chunks[0].content
    for chunk in chunks:
        source.write_bytes(original)
        meta = chunk.metadata
        raw_lines = text.splitlines()
        raw_span = "\n".join(raw_lines[meta.start_line - 1 : meta.end_line])
        assert meta.source_span_hash == hashlib.sha256(raw_span.encode()).hexdigest()
        args = [source, meta.start_line, meta.end_line]
        if operation != "remove_lines":
            args.append("NEW BODY")
        getattr(memory_writer, operation)(*args, expected_source_span_hash=meta.source_span_hash)
        assert source.read_bytes() != original


async def test_range_and_hash_refresh_roll_back_together(indexed_source):
    comp, source, chunk = indexed_source
    before = await comp.storage.get_chunk(chunk.id)
    changed = dataclasses.replace(
        chunk,
        metadata=dataclasses.replace(
            chunk.metadata, start_line=20, end_line=24, source_span_hash="a" * 64
        ),
    )
    with pytest.raises(RuntimeError, match="abort"):
        async with comp.storage.transaction():
            assert await comp.storage.update_chunk_line_ranges([changed]) == 1
            raise RuntimeError("abort")
    after = await comp.storage.get_chunk(chunk.id)
    assert after == before
    assert source.read_text() == ORIGINAL


async def test_repeated_source_ranges_are_hashed_once_per_snapshot(
    bm25_only_components, monkeypatch
):
    comp, directory = bm25_only_components
    source = directory / "long-line.md"
    text = "A sentence with many words. " * 3000
    spy = Mock(wraps=source_span_hash)
    monkeypatch.setattr("memtomem.source_provenance.source_span_hash", spy)

    for snapshot in (text, text.replace("sentence", "paragraph")):
        spy.reset_mock()
        chunks = comp.index_engine.chunk_content(source, snapshot)
        ranges = {(c.metadata.start_line, c.metadata.end_line) for c in chunks}
        assert len(chunks) > len(ranges)
        assert spy.call_count == len(ranges)
        for chunk in chunks:
            assert chunk.metadata.source_span_hash == source_span_hash(
                snapshot.splitlines(), chunk.metadata.start_line, chunk.metadata.end_line
            )


async def test_import_does_not_trust_external_source_evidence(indexed_source):
    from memtomem.tools.export_import import _chunk_to_dict, _dict_to_chunk

    comp, source, chunk = indexed_source
    record = _chunk_to_dict(chunk)
    assert "source_span_hash" not in record
    record["source_span_hash"] = chunk.metadata.source_span_hash
    imported, _ = _dict_to_chunk(record, namespace_override=None)
    assert imported.metadata.source_span_hash is None
    imported.id = chunk.id
    await comp.storage.upsert_chunks([imported])
    _assert_stale(await _request(comp, "mcp_edit", imported.id))
    assert source.read_text() == ORIGINAL


@pytest.mark.parametrize("surface", ["mcp_edit", "mcp_delete", "web_edit"])
async def test_provenance_exception_after_write_still_rolls_back(
    indexed_source, monkeypatch, surface
):
    comp, source, chunk = indexed_source
    original_index = comp.index_engine.index_file
    observed = []

    async def fail_first(*args, **kwargs):
        observed.append(source.read_bytes())
        if len(observed) == 1:
            raise StaleSourceProvenanceError("late failure")
        return await original_index(*args, **kwargs)

    monkeypatch.setattr(comp.index_engine, "index_file", fail_first)
    result = await _request(comp, surface, chunk.id)
    if isinstance(result, str):
        assert "rolled back" in result
    else:
        assert result.status_code == 409
    assert len(observed) == 2
    assert observed[0] != ORIGINAL.encode()
    assert observed[1] == source.read_bytes() == ORIGINAL.encode()
