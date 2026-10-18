"""A redaction-blocked re-index holds the file's older chunks (#2629).

The guard refuses a file whose new content looks like it carries a secret.
Its chunks from the last clean index used to stay searchable, including text
the same edit deleted. They are now held: hidden from retrieval, kept on disk,
and visible again after the next clean index pass.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from memtomem.config import IndexingConfig
from memtomem.indexing.engine import PrivacyRejection
from memtomem.indexing.watcher import FileWatcher
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD

# Assembled at runtime so the literal never sits in the tree as a key.
_SECRET = "AKIA" + "IOSFODNN7EXAMPLE"


def _entries(*tokens: str, secret: bool = False) -> str:
    body = "".join(f"## Entry {tok}\n\n{tok} remembered fact.\n\n" for tok in tokens)
    if secret:
        body += f"## Credentials\n\naws key {_SECRET}\n\n"
    return "# Notes\n\n" + body


@pytest.fixture
def engine(components):
    embedder = AsyncMock()
    embedder.embed_texts = AsyncMock(side_effect=lambda texts, **_: [[0.1] * 1024 for _ in texts])
    embedder.dimension = 1024
    components.index_engine._embedder = embedder
    return components.index_engine


async def _found(components, token: str) -> int:
    return len(await components.storage.bm25_search(token))


def _hold_reason(components, path: Path) -> str | None:
    from memtomem.storage.sqlite_helpers import norm_path

    row = (
        components.storage._get_db()
        .execute("SELECT reason FROM held_sources WHERE source_file=?", (norm_path(path),))
        .fetchone()
    )
    return row[0] if row else None


async def _block(engine, path: Path, *tokens: str) -> PrivacyRejection:
    path.write_text(_entries(*tokens, secret=True), encoding="utf-8")
    with pytest.raises(PrivacyRejection) as caught:
        await engine.index_file(path)
    return caught.value


async def test_a_blocked_reindex_stops_serving_the_old_chunks(components, engine, memory_dir):
    path = memory_dir / "notes.md"
    path.write_text(_entries("tokA1", "tokB2", "tokC3"), encoding="utf-8")
    assert not (await engine.index_file(path)).errors
    assert all([await _found(components, t) for t in ("tokA1", "tokB2", "tokC3")])

    # Control: a clean edit deletes what it removed.
    path.write_text(_entries("tokA1", "tokC3"), encoding="utf-8")
    assert not (await engine.index_file(path)).errors
    assert await _found(components, "tokB2") == 0

    # The issue's edit: drop tokA1 and add a key in the same change.
    rejection = await _block(engine, path, "tokC3")
    assert await _found(components, "tokA1") == 0
    assert await _found(components, "tokC3") == 0
    assert rejection.held is True
    assert path in await components.storage.get_held_sources()
    assert _hold_reason(components, path) == REDACTION_BLOCKED_HOLD
    # Held, not deleted.
    assert await components.storage.get_chunk_hashes(path)


async def test_fixing_the_file_lifts_the_hold(components, engine, memory_dir):
    path = memory_dir / "notes.md"
    path.write_text(_entries("tokA1", "tokC3"), encoding="utf-8")
    await engine.index_file(path)
    await _block(engine, path, "tokC3")

    path.write_text(_entries("tokC3"), encoding="utf-8")
    assert not (await engine.index_file(path)).errors
    assert path not in await components.storage.get_held_sources()
    assert await _found(components, "tokC3") == 1
    assert await _found(components, "tokA1") == 0


async def test_restoring_the_exact_clean_bytes_lifts_the_hold_on_a_receipt_hit(
    components, engine, memory_dir, monkeypatch
):
    path = memory_dir / "notes.md"
    clean = _entries("tokA1", "tokC3")
    path.write_text(clean, encoding="utf-8")
    await engine.index_file(path)
    await _block(engine, path, "tokA1", "tokC3")

    chunker = Mock(wraps=engine.chunk_content)
    monkeypatch.setattr(engine, "chunk_content", chunker)
    path.write_text(clean, encoding="utf-8")
    assert not (await engine.index_file(path)).errors
    # The receipt from the last clean pass was reused: nothing was chunked.
    chunker.assert_not_called()
    assert path not in await components.storage.get_held_sources()
    assert await _found(components, "tokA1") == 1


async def test_a_file_blocked_on_its_first_index_gets_no_hold(components, engine, memory_dir):
    path = memory_dir / "fresh.md"
    await _block(engine, path, "tokA1")
    assert path not in await components.storage.get_held_sources()
    assert _hold_reason(components, path) is None


async def test_bulk_index_holds_the_blocked_file_and_indexes_the_rest(
    components, engine, memory_dir
):
    blocked = memory_dir / "blocked.md"
    clean = memory_dir / "clean.md"
    blocked.write_text(_entries("tokA1"), encoding="utf-8")
    await engine.index_file(blocked)
    blocked.write_text(_entries("tokA1", secret=True), encoding="utf-8")
    clean.write_text(_entries("tokZ9"), encoding="utf-8")

    stats = await engine.index_path(memory_dir)
    assert stats.blocked_files == 1
    assert await _found(components, "tokA1") == 0
    assert await _found(components, "tokZ9") == 1
    assert _hold_reason(components, blocked) == REDACTION_BLOCKED_HOLD
    assert not stats.retryable_errors


async def test_a_failed_hold_is_reported_and_retryable(components, engine, memory_dir, monkeypatch):
    path = memory_dir / "notes.md"
    path.write_text(_entries("tokA1"), encoding="utf-8")
    await engine.index_file(path)

    async def failing_hold(source_file, reason):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(components.storage, "hold_source", failing_hold)
    rejection = await _block(engine, path, "tokA1")
    assert rejection.held is False

    stats = await engine.index_path(memory_dir)
    assert stats.blocked_files == 1
    unhidden = [e for e in stats.retryable_errors if "could not hide" in e]
    assert unhidden == ["notes.md: could not hide the indexed chunks of this blocked file; retry"]
    # The blocked entry keeps the shape the CLI's blocked summary recognises.
    from memtomem.cli._index_progress import _REDACTION_BLOCKED_RE

    assert [e for e in stats.errors if _REDACTION_BLOCKED_RE.search(e)] == [
        e for e in stats.errors if e.startswith("notes.md: redaction_blocked (")
    ]
    assert unhidden[0] in stats.errors
    assert not _REDACTION_BLOCKED_RE.search(unhidden[0])


async def test_an_availability_reason_does_not_replace_a_block(components, engine, memory_dir):
    path = memory_dir / "notes.md"
    path.write_text(_entries("tokA1"), encoding="utf-8")
    await engine.index_file(path)
    await _block(engine, path, "tokA1")

    for reason in ("watch_root_removed", "scan_missing", "scan_unavailable"):
        assert await components.storage.hold_source(path, reason) is False
        assert _hold_reason(components, path) == REDACTION_BLOCKED_HOLD


async def test_blocking_an_already_held_source_fences_older_releases(
    components, engine, memory_dir
):
    path = memory_dir / "notes.md"
    path.write_text(_entries("tokA1"), encoding="utf-8")
    await engine.index_file(path)
    assert await components.storage.hold_source(path, "scan_unavailable")
    before = await components.storage.source_hold_snapshot(path)

    await _block(engine, path, "tokA1")
    assert _hold_reason(components, path) == REDACTION_BLOCKED_HOLD
    assert await components.storage.source_hold_snapshot(path) != before
    # A release prepared before the block cannot lift it.
    assert not await components.storage.release_source_hold(path, expected_generation=before)
    assert path in await components.storage.get_held_sources()


async def test_presence_alone_does_not_lift_a_block(components, engine, memory_dir):
    blocked = memory_dir / "blocked.md"
    missing = memory_dir / "missing.md"
    for path in (blocked, missing):
        path.write_text(_entries("tokA1"), encoding="utf-8")
        await engine.index_file(path)
    await _block(engine, blocked, "tokA1")
    assert await components.storage.hold_source(missing, "scan_missing")

    assert not await components.storage.release_source_hold(blocked, validated=False)
    # A current generation does not override the content veto.
    current = await components.storage.source_hold_snapshot(blocked)
    assert not await components.storage.release_source_hold(
        blocked, validated=False, expected_generation=current
    )
    assert blocked in await components.storage.get_held_sources()
    # Control: an availability hold still lifts on presence.
    assert await components.storage.release_source_hold(missing, validated=False)


def test_a_failed_hold_makes_the_refusal_retryable_for_the_debounce_queue(tmp_path):
    from memtomem.indexing import debounce

    def drain(rejection):
        entries = {"p": debounce.QueueEntry(first_seen=0.0, last_seen=0.0)}
        result = debounce.DrainResult()
        debounce._record_failure(entries, "p", entries["p"], rejection, result)
        return entries, result

    held = PrivacyRejection(path=tmp_path / "a.md", hit_count=1, scope="user", decision="blocked")
    held.held = True
    entries, result = drain(held)
    # A settled block is permanent: re-running changes nothing.
    assert entries == {} and result.dropped

    unheld = PrivacyRejection(path=tmp_path / "a.md", hit_count=1, scope="user", decision="blocked")
    unheld.mark_unheld()
    entries, result = drain(unheld)
    assert "p" in entries and result.retryable_errors
    assert "could not hide the indexed chunks" in result.retryable_errors[0][1]


async def _run_recheck_until(watcher, done) -> None:
    task = asyncio.create_task(watcher._recheck_held_loop())
    try:
        for _ in range(500):
            if await done():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("recheck loop never reached the expected state")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_recheck_of_a_still_blocked_file_keeps_the_hold_quietly(
    components, engine, memory_dir, caplog
):
    path = memory_dir / "notes.md"
    path.write_text(_entries("tokA1"), encoding="utf-8")
    await engine.index_file(path)
    await _block(engine, path, "tokA1")

    attempted = asyncio.Event()
    real_index_file = engine.index_file

    async def counting_index_file(*args, **kwargs):
        try:
            return await real_index_file(*args, **kwargs)
        finally:
            attempted.set()

    watcher = FileWatcher(engine, components.config.indexing)
    engine.index_file = counting_index_file
    caplog.set_level(logging.DEBUG, logger="memtomem.indexing.watcher")

    async def handled() -> bool:
        # The handler runs before the loop next yields, so one more turn of
        # the event loop after the attempt has finished covers it.
        if not attempted.is_set():
            return False
        await asyncio.sleep(0)
        return True

    await _run_recheck_until(watcher, handled)
    assert path in await components.storage.get_held_sources()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("still redaction-blocked" in r.getMessage() for r in caplog.records)


async def test_recheck_outside_the_roots_does_not_lift_a_block(
    components, engine, memory_dir, tmp_path
):
    blocked = memory_dir / "blocked.md"
    missing = memory_dir / "missing.md"
    for path in (blocked, missing):
        path.write_text(_entries("tokA1"), encoding="utf-8")
        await engine.index_file(path)
    await _block(engine, blocked, "tokA1")
    assert await components.storage.hold_source(missing, "scan_missing")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    watcher = FileWatcher(engine, IndexingConfig(memory_dirs=[elsewhere]))

    async def control_released() -> bool:
        return missing not in await components.storage.get_held_sources()

    await _run_recheck_until(watcher, control_released)
    assert blocked in await components.storage.get_held_sources()
    assert _hold_reason(components, blocked) == REDACTION_BLOCKED_HOLD


async def test_watcher_retries_a_block_whose_hold_failed(
    components, engine, memory_dir, monkeypatch
):
    path = memory_dir / "notes.md"
    path.write_text(_entries("tokA1"), encoding="utf-8")
    await engine.index_file(path)
    path.write_text(_entries("tokA1", secret=True), encoding="utf-8")
    watcher = FileWatcher(engine, components.config.indexing)
    assert await watcher._reindex(path) is None

    async def failing_hold(source_file, reason):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(components.storage, "hold_source", failing_hold)
    assert await watcher._reindex(path) == path
