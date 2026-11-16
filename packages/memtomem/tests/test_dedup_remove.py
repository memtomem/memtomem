"""``mem_dedup_remove``: a duplicate removal checks both members against their files (#3419).

Each member's file and rows must still match its source index receipt, the two must be in
different files, and neither file may have another hard-linked name. Receipts are written
only by a plain index pass (not ``force``, not the guarded re-index a CRUD write runs), so
every fixture indexes with a plain ``index_file`` call.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools import dedup_decay

BODY = "Alarm is enabled and pages the on-call engineer at night.\n"


def _note(device: str) -> str:
    return f"# {device}\n\n## Alarm\n\n{BODY}"


async def _index(comp, source: Path, **kwargs) -> None:
    stats = await comp.index_engine.index_file(source, **kwargs)
    assert not stats.errors, stats.errors


async def _alarm_chunk(comp, source: Path):
    rows = await comp.storage.list_chunks_by_source(source)
    (chunk,) = [c for c in rows if c.metadata.heading_hierarchy[-1:] == ("## Alarm",)]
    return chunk


@pytest.fixture
async def pair(bm25_only_components):
    """Two files holding the same ``## Alarm`` section, both indexed with a receipt."""
    comp, mem_dir = bm25_only_components
    removed_file, kept_file = mem_dir / "a.md", mem_dir / "b.md"
    removed_file.write_text(_note("Router-X"), encoding="utf-8")
    kept_file.write_text(_note("Router-X"), encoding="utf-8")
    for source in (removed_file, kept_file):
        await _index(comp, source)
    removed = await _alarm_chunk(comp, removed_file)
    kept = await _alarm_chunk(comp, kept_file)
    return comp, removed_file, kept_file, removed, kept


async def _remove(comp, remove_id, keep_id, **kwargs) -> str:
    ctx = StubCtx(AppContext.from_components(comp))
    return await dedup_decay.mem_dedup_remove(str(remove_id), str(keep_id), ctx=ctx, **kwargs)


def _rows(comp):
    return comp.storage._get_db().execute("SELECT * FROM chunks ORDER BY id").fetchall()


def _snapshot(comp, *files: Path):
    return [f.read_bytes() for f in files], _rows(comp)


async def test_both_fresh_removes_the_lines_and_keeps_the_other(pair):
    comp, removed_file, kept_file, removed, kept = pair
    kept_bytes = kept_file.read_bytes()

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith(f"Memory deleted from {removed_file}"), result
    assert BODY not in removed_file.read_text(encoding="utf-8")
    assert kept_file.read_bytes() == kept_bytes
    assert await comp.storage.get_chunk(removed.id) is None
    assert await comp.storage.get_chunk(kept.id) is not None


async def test_a_kept_file_edited_since_it_was_indexed_is_refused(pair):
    """#3419's example: the keeper's enclosing heading changed and was not reindexed."""
    comp, removed_file, kept_file, removed, kept = pair
    kept_file.write_text(_note("Router-Y"), encoding="utf-8")
    before = _snapshot(comp, removed_file, kept_file)

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith(f"Error: the kept member's file {kept_file} changed since"), result
    assert "no lines were removed" in result and "mm index" in result
    assert _snapshot(comp, removed_file, kept_file) == before


async def test_the_removed_file_edited_outside_the_span_is_refused(pair):
    comp, removed_file, kept_file, removed, kept = pair
    removed_file.write_text(_note("Router-Z"), encoding="utf-8")
    before = _snapshot(comp, removed_file, kept_file)

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith(f"Error: the removed member's file {removed_file} changed"), result
    assert _snapshot(comp, removed_file, kept_file) == before


async def test_after_a_plain_reindex_a_different_heading_passes(pair):
    """The stated limit: equivalence is the reviewer's judgment, not this check's."""
    comp, removed_file, kept_file, removed, kept = pair
    kept_file.write_text(_note("Router-Y"), encoding="utf-8")
    await _index(comp, kept_file)
    assert (await comp.storage.get_chunk(kept.id)) is not None

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith("Memory deleted from"), result


async def test_a_keeper_without_a_receipt_is_refused_until_a_plain_reindex(
    bm25_only_components,
):
    comp, mem_dir = bm25_only_components
    removed_file, kept_file = mem_dir / "a.md", mem_dir / "b.md"
    removed_file.write_text(_note("Router-X"), encoding="utf-8")
    kept_file.write_text(_note("Router-X"), encoding="utf-8")
    await _index(comp, removed_file)
    await _index(comp, kept_file, force=True)  # a forced pass records no receipt
    removed = await _alarm_chunk(comp, removed_file)
    kept = await _alarm_chunk(comp, kept_file)
    before = _snapshot(comp, removed_file, kept_file)

    refused = await _remove(comp, removed.id, kept.id)
    assert refused.startswith(f"Error: the kept member's file {kept_file} has no index receipt")
    assert "not `--force`" in refused
    assert _snapshot(comp, removed_file, kept_file) == before

    await _index(comp, kept_file)
    assert (await _remove(comp, removed.id, kept.id)).startswith("Memory deleted from")


async def test_no_receipt_under_auto_summarize_names_the_setting(bm25_only_components):
    comp, mem_dir = bm25_only_components
    removed_file, kept_file = mem_dir / "a.md", mem_dir / "b.md"
    for source in (removed_file, kept_file):
        source.write_text(_note("Router-X"), encoding="utf-8")
    await _index(comp, removed_file)
    await _index(comp, kept_file, force=True)
    removed = await _alarm_chunk(comp, removed_file)
    kept = await _alarm_chunk(comp, kept_file)
    comp.config.indexing.auto_summarize = True

    result = await _remove(comp, removed.id, kept.id)

    assert "has no index receipt" in result
    assert "indexing.auto_summarize on record no receipt" in result
    assert "mm index" not in result


async def test_an_older_receipt_still_counts_under_auto_summarize(pair):
    comp, _removed_file, _kept_file, removed, kept = pair
    comp.config.indexing.auto_summarize = True

    assert (await _remove(comp, removed.id, kept.id)).startswith("Memory deleted from")


async def test_kept_rows_that_differ_from_the_receipt_are_refused(pair):
    comp, removed_file, kept_file, removed, kept = pair
    db = comp.storage._get_db()
    db.execute("UPDATE chunks SET tags=? WHERE id=?", ('["tampered"]', str(kept.id)))
    db.commit()
    before = _snapshot(comp, removed_file, kept_file)

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith(f"Error: the kept member's file {kept_file} has stored rows"), result
    assert _snapshot(comp, removed_file, kept_file) == before


async def test_a_deleted_kept_file_is_refused(pair):
    comp, removed_file, kept_file, removed, kept = pair
    kept_file.unlink()
    removed_before = removed_file.read_bytes()

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith(f"Error: the kept member's file {kept_file} cannot be opened")
    assert removed_file.read_bytes() == removed_before


async def test_a_pair_in_one_file_is_refused_before_any_lock(bm25_only_components, monkeypatch):
    from memtomem.server.tools import memory_crud

    comp, mem_dir = bm25_only_components

    def no_lock(*_args):
        raise AssertionError("the same-file refusal must come before the removal's lock")

    monkeypatch.setattr(memory_crud, "_locked_chunk", no_lock)
    source = mem_dir / "both.md"
    source.write_text(f"## One\n\n{BODY}\n## Two\n\n{BODY}", encoding="utf-8")
    await _index(comp, source)
    first, second = await comp.storage.list_chunks_by_source(source)
    before = _snapshot(comp, source)
    entries = sorted(p.name for p in mem_dir.iterdir())  # the index pass made its sidecar

    result = await _remove(comp, first.id, second.id)

    assert result.startswith(f"Error: both members are in one file ({source})"), result
    assert _snapshot(comp, source) == before
    assert sorted(p.name for p in mem_dir.iterdir()) == entries


@pytest.mark.parametrize("linked", ["removed", "kept"])
async def test_a_hard_linked_member_is_refused(pair, linked):
    comp, removed_file, kept_file, removed, kept = pair
    target = removed_file if linked == "removed" else kept_file
    try:
        os.link(target, target.with_name("alias.md"))
    except OSError as exc:  # a filesystem without hard links
        pytest.skip(f"no hard links here: {exc}")
    before = _snapshot(comp, removed_file, kept_file)

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith(f"Error: the {linked} member's file {target} has another hard-linked")
    assert _snapshot(comp, removed_file, kept_file) == before


async def test_a_held_keeper_is_refused(pair):
    comp, removed_file, kept_file, removed, kept = pair
    assert await comp.storage.hold_source(kept_file, "test")
    before = _snapshot(comp, removed_file, kept_file)

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith(f"Error: the kept member's file {kept_file} is held"), result
    assert "Clear the hold first" in result
    assert _snapshot(comp, removed_file, kept_file) == before


async def test_an_out_of_boundary_keeper_reads_as_not_found(pair, monkeypatch):
    comp, removed_file, kept_file, removed, kept = pair
    monkeypatch.setattr(dedup_decay, "in_boundary", lambda chunk, _b: chunk.id != kept.id)
    before = _snapshot(comp, removed_file, kept_file)

    assert await _remove(comp, removed.id, kept.id) == f"Error: chunk {kept.id} not found."
    assert _snapshot(comp, removed_file, kept_file) == before


async def test_ids(pair):
    comp, _removed_file, _kept_file, removed, kept = pair
    assert "same chunk" in await _remove(comp, removed.id, removed.id)
    assert (await _remove(comp, "nope", kept.id)).startswith("Error: invalid chunk ID format")
    missing = uuid4()
    assert await _remove(comp, removed.id, missing) == f"Error: chunk {missing} not found."


async def test_a_keeper_changed_after_the_check_is_not_seen(pair, monkeypatch):
    """The stated limit: the kept file is not locked, so a change after the check lands."""
    comp, removed_file, kept_file, removed, kept = pair
    check = dedup_decay._check_pair

    async def check_then_edit(*args):
        refusal = await check(*args)
        kept_file.write_text(_note("Router-Y"), encoding="utf-8")
        return refusal

    monkeypatch.setattr(dedup_decay, "_check_pair", check_then_edit)

    assert (await _remove(comp, removed.id, kept.id)).startswith("Memory deleted from")
    assert "Router-Y" in kept_file.read_text(encoding="utf-8")


async def test_a_kept_key_that_became_a_symlink_is_refused(pair, tmp_path):
    """Read through the link the bytes still match; the key no longer names the indexed file."""
    comp, removed_file, kept_file, removed, kept = pair
    elsewhere = tmp_path / "elsewhere.md"
    kept_file.rename(elsewhere)
    try:
        kept_file.symlink_to(elsewhere)
    except OSError as exc:  # no symlink privilege (Windows)
        pytest.skip(f"no symlinks here: {exc}")
    removed_before = removed_file.read_bytes()

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith(f"Error: the kept member's file {kept_file} now resolves"), result
    assert removed_file.read_bytes() == removed_before


async def test_a_keeper_leaving_the_boundary_during_the_check_reads_as_not_found(pair, monkeypatch):
    """The final fetch on the writer is screened too, not only the earlier ones."""
    comp, removed_file, kept_file, removed, kept = pair
    seen = []

    def screen(chunk, _boundary):
        if chunk.id != kept.id:
            return True
        seen.append(chunk.id)
        return len(seen) < 3  # the tool's probe, the precheck's fetch, then the final one

    monkeypatch.setattr(dedup_decay, "in_boundary", screen)
    before = _snapshot(comp, removed_file, kept_file)

    assert await _remove(comp, removed.id, kept.id) == f"Error: chunk {kept.id} not found."
    assert len(seen) == 3
    assert _snapshot(comp, removed_file, kept_file) == before


async def test_a_filesystem_without_file_identity_is_refused(pair, monkeypatch):
    comp, removed_file, kept_file, removed, kept = pair
    real = dedup_decay.read_stored_source

    def no_identity(key):
        read = real(key)
        return {**read, "inode": 0} if isinstance(read, dict) else read

    monkeypatch.setattr(dedup_decay, "read_stored_source", no_identity)
    before = _snapshot(comp, removed_file, kept_file)

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith(f"Error: the removed member's file {removed_file} is on a filesystem")
    assert _snapshot(comp, removed_file, kept_file) == before


@pytest.mark.parametrize("kind", ["fifo", "directory"])
async def test_a_kept_path_that_is_no_longer_a_regular_file_is_refused(pair, kind):
    comp, removed_file, kept_file, removed, kept = pair
    kept_file.unlink()
    if kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("no FIFOs here")
        os.mkfifo(kept_file)  # a blocking open would hang here, with no writer
    else:
        kept_file.mkdir()
    removed_before = removed_file.read_bytes()

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith(f"Error: the kept member's file {kept_file} is not a regular file")
    assert removed_file.read_bytes() == removed_before


async def test_a_file_larger_than_the_indexer_reads_is_refused(pair, monkeypatch):
    from memtomem.indexing import engine

    comp, removed_file, kept_file, removed, kept = pair
    monkeypatch.setattr(engine, "_MAX_INDEX_FILE_BYTES", 8)

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith(f"Error: the removed member's file {removed_file} is larger")


async def test_the_hash_matches_what_the_indexer_hashed(bm25_only_components):
    """CRLF line ends and an invalid byte: the read decodes as ``index_file`` does."""
    from memtomem.indexing.engine import read_stored_source
    from memtomem.indexing.source_receipt import receipt_as_stored

    comp, mem_dir = bm25_only_components
    source = mem_dir / "crlf.md"
    source.write_bytes(_note("Router-X").replace("\n", "\r\n").encode() + b"tail \xff\r\n")
    await _index(comp, source)
    key = str((await comp.storage.list_chunks_by_source(source))[0].metadata.source_file)

    receipt = receipt_as_stored(comp.storage._get_db(), key)
    read = read_stored_source(key)

    assert receipt is not None and isinstance(read, dict)
    assert read["content_hash"] == receipt[0]


async def test_a_pending_keeper_is_refused(pair):
    comp, removed_file, kept_file, removed, kept = pair
    assert comp.storage.queue_source_check_sync(kept_file)
    before = _snapshot(comp, removed_file, kept_file)

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith(f"Error: the kept member's file {kept_file} is held"), result
    assert "Clear the hold first" in result
    assert _snapshot(comp, removed_file, kept_file) == before


async def test_a_hold_committed_past_an_open_pool_statement_is_seen(pair, monkeypatch):
    from test_storage_read_routing import pool_reader_mid_statement

    comp, removed_file, kept_file, removed, kept = pair
    before = _snapshot(comp, removed_file, kept_file)
    with pool_reader_mid_statement(comp.storage, monkeypatch):
        assert await comp.storage.hold_source(kept_file, "test")
        result = await _remove(comp, removed.id, kept.id)

    assert result.startswith(f"Error: the kept member's file {kept_file} is held"), result
    assert "Clear the hold first" in result
    assert _snapshot(comp, removed_file, kept_file) == before


async def test_two_names_with_one_identity_are_one_file(pair, monkeypatch):
    """Equal nonzero identities under different lock keys, one link each: refused."""
    comp, removed_file, kept_file, removed, kept = pair
    real = dedup_decay.read_stored_source

    def same_identity(key):
        read = real(key)
        assert isinstance(read, dict)
        return {**read, "device": 7, "inode": 11, "links": 1}

    monkeypatch.setattr(dedup_decay, "read_stored_source", same_identity)
    before = _snapshot(comp, removed_file, kept_file)

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith("Error: mem_dedup_remove refused: the store holds"), result
    assert "under two spellings of its path" in result
    assert _snapshot(comp, removed_file, kept_file) == before


async def test_a_legacy_nfd_key_does_not_find_its_nfc_receipt(bm25_only_components):
    """The receipt is read by the key as stored; a normalized lookup would find it."""
    import unicodedata

    comp, mem_dir = bm25_only_components
    removed_file, kept_file = mem_dir / "a.md", mem_dir / "café.md"
    for source in (removed_file, kept_file):
        source.write_text(_note("Router-X"), encoding="utf-8")
        await _index(comp, source)
    removed = await _alarm_chunk(comp, removed_file)
    kept = await _alarm_chunk(comp, kept_file)
    nfc_key = str(kept.metadata.source_file)
    nfd_key = unicodedata.normalize("NFD", nfc_key)
    assert nfd_key != nfc_key
    if not Path(nfd_key).exists():  # a filesystem that does not fold Unicode forms
        Path(nfd_key).write_bytes(kept_file.read_bytes())
    db = comp.storage._get_db()
    db.execute("UPDATE chunks SET source_file=? WHERE source_file=?", (nfd_key, nfc_key))
    db.commit()
    assert db.execute(
        "SELECT 1 FROM source_index_receipts WHERE source_file=?", (nfc_key,)
    ).fetchone()
    before = _snapshot(comp, removed_file, kept_file)

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith(f"Error: the kept member's file {nfd_key} has no index receipt")
    assert _snapshot(comp, removed_file, kept_file) == before


def test_an_oversized_file_is_refused_before_it_is_read(tmp_path, monkeypatch):
    from memtomem.indexing import engine

    source = tmp_path / "big.md"
    source.write_text("x" * 64, encoding="utf-8")
    monkeypatch.setattr(engine, "_MAX_INDEX_FILE_BYTES", 8)

    def no_read(*_args):
        raise AssertionError("an oversized file is refused from its fstat size, unread")

    monkeypatch.setattr(engine.os, "read", no_read)

    assert engine.read_stored_source(str(source)) == "is larger than the indexer reads"


async def test_a_busy_writer_is_a_retryable_refusal_worded_for_this_tool(pair, monkeypatch):
    from memtomem.errors import StorageError

    comp, removed_file, kept_file, removed, kept = pair
    real_transaction = comp.storage.transaction

    def busy_transaction(*args, **kwargs):
        if kwargs.get("wait") is not None and dedup_busy["armed"]:
            raise StorageError("database is locked")
        return real_transaction(*args, **kwargs)

    dedup_busy = {"armed": False}
    real_check = dedup_decay._check_pair

    async def armed_check(*args):
        dedup_busy["armed"] = True
        try:
            return await real_check(*args)
        finally:
            dedup_busy["armed"] = False

    monkeypatch.setattr(comp.storage, "transaction", busy_transaction)
    monkeypatch.setattr(dedup_decay, "_check_pair", armed_check)
    monkeypatch.setattr(dedup_decay, "is_writer_busy", lambda _exc: True)
    before = _snapshot(comp, removed_file, kept_file)

    result = await _remove(comp, removed.id, kept.id)

    assert result.startswith("Error (retryable): "), result
    assert "writer is busy; no lines were removed" in result, result
    assert _snapshot(comp, removed_file, kept_file) == before


async def test_one_file_under_two_stored_spellings_gets_the_twin_remedy(bm25_only_components):
    comp, mem_dir = bm25_only_components
    source = mem_dir / "note.md"
    source.write_text(_note("Router-X"), encoding="utf-8")
    await _index(comp, source)
    twin = str(source.with_name("NOTE.md"))
    db = comp.storage._get_db()
    db.execute("UPDATE chunks SET source_file=? WHERE source_file=?", (twin, str(source)))
    db.commit()
    await _index(comp, source, force=True)
    original = await _alarm_chunk(comp, source)
    (other,) = [
        c
        for c in await comp.storage.list_chunks_by_source(Path(twin))
        if c.metadata.heading_hierarchy[-1:] == ("## Alarm",)
    ]
    before = _snapshot(comp, source)

    result = await _remove(comp, original.id, other.id)

    assert result.startswith("Error: mem_dedup_remove refused: the store holds note.md"), result
    assert _snapshot(comp, source) == before


async def test_the_files_are_read_inside_the_writer_transaction(pair, monkeypatch):
    """One moment: no commit can land between the file reads and the row reads."""
    comp, _removed_file, _kept_file, removed, kept = pair
    real = dedup_decay._read_members
    owners = []

    def recording(keys):
        owners.append(comp.storage._transaction_owner)
        return real(keys)

    monkeypatch.setattr(dedup_decay, "_read_members", recording)

    assert (await _remove(comp, removed.id, kept.id)).startswith("Memory deleted from")
    assert owners and owners[0] is not None
