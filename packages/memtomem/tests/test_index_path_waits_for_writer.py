"""A single-file index waits out another task's write transaction (#3173).

``_get_db()`` refuses at once while another task owns ``storage.transaction()``.
Before this, an index of an existing file failed at its first writer read, and
a missing or unreadable file failed at its hold. Each writer access in the
index path now waits, up to ``WRITER_WAIT_S``, when an ``await`` can run
between it and the previous wait.

Each test installs the competing owner at the suspension point just before the
site it covers, so the site is reached while the owner holds the writer. It
then checks the index is queued on the writer lock, releases the owner, and
checks the site's outcome. The owner always uses the store's own
``transaction``, captured before any test patches it.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import memtomem.indexing.engine as engine_mod
from memtomem.errors import RetryableError, StorageError, TransactionOwnedError
from memtomem.indexing.engine import PrivacyRejection
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD, SCOPE_LOST_HOLD
from memtomem.storage.sqlite_helpers import norm_path

# Assembled at runtime so the literal never sits in the tree as a key.
_SECRET = "AKIA" + "IOSFODNN7EXAMPLE"
_NOTE = "# Note\n\n## Plans\n\nalpha falcon body\n"


class _Owner:
    """Another task that owns the writer until :meth:`finish`."""

    def __init__(self, storage) -> None:
        self._transaction = storage.transaction
        self._storage = storage
        self.entered, self._release = asyncio.Event(), asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        async with self._transaction():
            self.entered.set()
            await self._release.wait()

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run())
        await asyncio.wait_for(self.entered.wait(), 5)

    async def finish(self) -> None:
        self._release.set()
        if self.task is not None:
            await self.task


def _queued(storage) -> bool:
    """Whether a task is queued on the writer lock (test-only peek at asyncio)."""
    return any(not w.cancelled() for w in (storage._writer_lock._waiters or ()))


async def _overlap(storage, owner: _Owner, call, *, while_queued=None):
    """Run ``call`` while ``owner`` holds (or will take) the writer; return its result.

    The call must end up queued on the writer lock while the owner holds it.
    ``while_queued`` then runs, before the owner is released. The owner is
    released before the call is awaited, so a call refused at once fails the
    test with its own error and no transaction stays open.
    """
    task = asyncio.ensure_future(call)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not (owner.entered.is_set() and _queued(storage)) and not task.done():
        if loop.time() > deadline:
            break
        await asyncio.sleep(0.005)
    queued = owner.entered.is_set() and _queued(storage) and not task.done()
    if queued and while_queued is not None:
        while_queued()
    await owner.finish()
    result = await asyncio.wait_for(task, 10)
    assert queued, "the call never queued on the writer while the owner held it"
    return result


def _hold_reason(storage, path: Path) -> str | None:
    row = (
        storage._get_db()
        .execute("SELECT reason FROM held_sources WHERE source_file=?", (norm_path(path),))
        .fetchone()
    )
    return row[0] if row else None


async def _texts(storage, path: Path) -> list[str]:
    return [c.content for c in await storage.list_chunks_by_source(path)]


@pytest.fixture
def engine(components):
    """A real store with a stub embedder of positive dimension (receipts need vectors)."""
    embedder = AsyncMock()
    embedder.embed_texts = AsyncMock(side_effect=lambda texts, **_: [[0.1] * 1024 for _ in texts])
    embedder.dimension = 1024
    components.index_engine._embedder = embedder
    return components.index_engine


async def _indexed(engine, path: Path, text: str = _NOTE) -> None:
    path.write_text(text, encoding="utf-8")
    stats = await engine.index_file(path)
    assert not stats.errors, stats.errors
    assert stats.indexed_chunks > 0


# ---- an owner already holding the writer when the index starts ------------


async def test_an_emptied_file_is_cleared_once_the_owner_finishes(components, engine, memory_dir):
    """The measured failure: refused at the scope read, the row stayed searchable."""
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    path.write_text("", encoding="utf-8")
    owner = _Owner(components.storage)
    await owner.start()

    stats = await _overlap(components.storage, owner, engine.index_file(path))

    assert not stats.errors
    assert stats.deleted_chunks > 0
    assert await _texts(components.storage, path) == []


async def test_an_edited_file_is_reindexed_once_the_owner_finishes(components, engine, memory_dir):
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    path.write_text("# Note\n\n## Plans\n\nbeta heron body\n", encoding="utf-8")
    owner = _Owner(components.storage)
    await owner.start()

    stats = await _overlap(components.storage, owner, engine.index_file(path))

    assert not stats.errors
    assert any("heron" in t for t in await _texts(components.storage, path))
    assert not any("falcon" in t for t in await _texts(components.storage, path))


async def test_a_deleted_file_is_held_once_the_owner_finishes(components, engine, memory_dir):
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    path.unlink()
    owner = _Owner(components.storage)
    await owner.start()

    stats = await _overlap(components.storage, owner, engine.index_file(path))

    assert not stats.errors
    assert _hold_reason(components.storage, path) == "source_missing"


async def test_a_retained_missing_file_is_shown_once_the_owner_finishes(
    components, engine, memory_dir
):
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    path.unlink()
    assert not (await engine.index_file(path)).errors
    assert _hold_reason(components.storage, path) == "source_missing"
    engine._config.retain_missing_dirs = [memory_dir]
    owner = _Owner(components.storage)
    await owner.start()

    stats = await _overlap(components.storage, owner, engine.index_file(path))

    assert not stats.errors
    assert _hold_reason(components.storage, path) is None


async def test_a_stat_failure_is_held_once_the_owner_finishes(
    components, engine, memory_dir, monkeypatch
):
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    real_stat = Path.stat

    def stat(self, *args, **kwargs):
        if self == path:
            raise OSError(errno.EIO, "stale mount")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    owner = _Owner(components.storage)
    await owner.start()

    await _overlap(components.storage, owner, engine.index_file(path))

    assert _hold_reason(components.storage, path) == f"stat_error:{errno.EIO}"


async def test_a_read_failure_is_held_once_the_owner_finishes(
    components, engine, memory_dir, monkeypatch
):
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):
        if self == path:
            raise OSError(errno.EIO, "stale mount")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    owner = _Owner(components.storage)
    await owner.start()

    await _overlap(components.storage, owner, engine.index_file(path))

    assert _hold_reason(components.storage, path) == f"read_error:{errno.EIO}"


async def test_a_file_replaced_by_a_directory_is_cleared_once_the_owner_finishes(
    components, engine, memory_dir
):
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    path.unlink()
    path.mkdir()
    owner = _Owner(components.storage)
    await owner.start()

    stats = await _overlap(components.storage, owner, engine.index_file(path))

    assert stats.deleted_chunks > 0
    assert await _texts(components.storage, path) == []


async def test_a_scope_loss_reindex_holds_once_the_owner_finishes(bm25_only_components, tmp_path):
    """Unregistered project rows are held, not rewritten as user rows, after the scope wait."""
    comp, _ = bm25_only_components
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    local = (root / ".memtomem" / "memories.local").resolve()
    local.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [local]
    target = local / "note.md"
    target.write_text(_NOTE, encoding="utf-8")
    assert not (await comp.index_engine.index_file(target)).errors
    comp.config.indexing.project_memory_dirs = []
    owner = _Owner(comp.storage)
    await owner.start()

    stats = await _overlap(comp.storage, owner, comp.index_engine.index_file(target, force=True))

    assert stats.scope_held
    assert _hold_reason(comp.storage, target) == SCOPE_LOST_HOLD


async def test_a_redaction_blocked_reindex_holds_once_the_owner_finishes(
    components, engine, memory_dir
):
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    path.write_text(_NOTE + f"\n## Credentials\n\naws key {_SECRET}\n", encoding="utf-8")
    owner = _Owner(components.storage)
    await owner.start()

    with pytest.raises(PrivacyRejection) as caught:
        await _overlap(components.storage, owner, engine.index_file(path))

    assert caught.value.held is True
    assert _hold_reason(components.storage, path) == REDACTION_BLOCKED_HOLD


async def test_a_refused_mutation_holds_once_the_owner_finishes(components, engine, memory_dir):
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    preflight = engine.preflight_mutation(
        path, (_NOTE + f"\naws key {_SECRET}\n").encode("utf-8"), source_key=norm_path(path)
    )
    assert preflight.rejection is not None
    owner = _Owner(components.storage)
    await owner.start()

    rejection = await _overlap(components.storage, owner, engine.refuse_mutation(path, preflight))

    assert rejection.held is True
    assert _hold_reason(components.storage, path) == REDACTION_BLOCKED_HOLD


async def test_a_redaction_hold_that_times_out_is_still_reported_and_retryable(
    components, engine, memory_dir, monkeypatch
):
    """A hold that cannot wait long enough does not mask the refusal."""
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    path.write_text(_NOTE + f"\naws key {_SECRET}\n", encoding="utf-8")
    monkeypatch.setattr(engine_mod, "WRITER_WAIT_S", 0.05)
    owner = _Owner(components.storage)
    await owner.start()
    try:
        with pytest.raises(PrivacyRejection) as caught:
            await engine.index_file(path)
    finally:
        await owner.finish()

    assert caught.value.held is False
    assert caught.value.retryable
    assert _hold_reason(components.storage, path) is None


# ---- an owner arriving at a suspension point mid-index ---------------------


async def test_a_receipt_hit_needs_no_writer_after_its_source_check(
    components, engine, memory_dir, monkeypatch
):
    """An owner arriving during the source check does not hold up a receipt hit.

    The hit reads its chunk ids with the receipt, under the scope read's wait,
    so nothing reaches the writer after the check: a wait there would let the
    file change after it was judged unchanged.
    """
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    stored_ids = sorted(str(c.id) for c in await components.storage.list_chunks_by_source(path))
    owner = _Owner(components.storage)
    real_check, real_index = engine._source_unchanged, engine._index_file
    results: list[dict] = []

    async def source_unchanged(*args, **kwargs):
        await owner.start()
        return await real_check(*args, **kwargs)

    async def index_file(*args, **kwargs):
        results.append(await real_index(*args, **kwargs))
        return results[-1]

    monkeypatch.setattr(engine, "_source_unchanged", source_unchanged)
    monkeypatch.setattr(engine, "_index_file", index_file)
    try:
        # Completes while the owner still holds the writer.
        stats = await asyncio.wait_for(engine.index_file(path), 5)
    finally:
        await owner.finish()

    assert owner.task is not None, "the receipt path was not taken"
    assert not stats.errors
    assert stats.skipped_chunks > 0 and stats.indexed_chunks == 0
    assert sorted(str(i) for i in results[0]["unchanged_chunk_ids"]) == stored_ids


async def test_a_held_source_is_released_after_a_clean_pass_that_overlaps(
    components, engine, memory_dir, monkeypatch
):
    """The release runs after the chunk write; an owner arriving between them is waited out."""
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    assert await components.storage.hold_source(path, "source_missing")
    owner = _Owner(components.storage)
    real = engine._index_file

    async def index_file_then_compete(*args, **kwargs):
        result = await real(*args, **kwargs)
        await owner.start()
        return result

    monkeypatch.setattr(engine, "_index_file", index_file_then_compete)

    stats = await _overlap(components.storage, owner, engine.index_file(path))

    assert not stats.errors
    assert _hold_reason(components.storage, path) is None


# ---- a path relinked while the index waits keeps its own key --------------
#
# Each wait leaves time for the path to be swapped for a symbolic link to
# another indexed source. Every key is taken before the wait, where the code
# without the wait took it, so the link's target is never touched.


def _symlink_or_skip(tmp_path: Path) -> None:
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(tmp_path)
    except OSError as exc:  # Windows without symlink privilege
        pytest.skip(f"cannot create symlink: {exc}")
    probe.unlink()


def _relink(path: Path, target: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        path.rmdir()
    else:
        path.unlink()
    path.symlink_to(target)


def _rows_under(storage, key: str) -> int:
    return (
        storage._get_db()
        .execute("SELECT COUNT(*) FROM chunks WHERE source_file=?", (key,))
        .fetchone()[0]
    )


def _reason_under(storage, key: str) -> str | None:
    row = (
        storage._get_db()
        .execute("SELECT reason FROM held_sources WHERE source_file=?", (key,))
        .fetchone()
    )
    return row[0] if row else None


_OTHER = "# B\n\n## S\n\nbeta heron\n"


async def test_a_non_regular_file_relinked_during_the_wait_deletes_only_its_own_rows(
    components, engine, memory_dir, tmp_path
):
    _symlink_or_skip(tmp_path)
    path, other = memory_dir / "a.md", memory_dir / "b.md"
    await _indexed(engine, path)
    await _indexed(engine, other, _OTHER)
    a_key, b_key = norm_path(path), norm_path(other)
    path.unlink()
    path.mkdir()
    owner = _Owner(components.storage)
    await owner.start()

    await _overlap(
        components.storage,
        owner,
        engine.index_file(path),
        while_queued=lambda: _relink(path, other),
    )

    assert _rows_under(components.storage, a_key) == 0
    assert _rows_under(components.storage, b_key) > 0


async def test_a_hold_release_relinked_during_the_wait_lifts_only_its_own_hold(
    components, engine, memory_dir, tmp_path, monkeypatch
):
    """Both sources held at the same generation: only the one indexed may be released."""
    _symlink_or_skip(tmp_path)
    path, other = memory_dir / "a.md", memory_dir / "b.md"
    await _indexed(engine, path)
    await _indexed(engine, other, _OTHER)
    a_key, b_key = norm_path(path), norm_path(other)
    assert await components.storage.hold_source(path, "source_missing")
    assert await components.storage.hold_source(other, "source_missing")
    assert await components.storage.source_hold_snapshot(path) == (
        await components.storage.source_hold_snapshot(other)
    )
    owner = _Owner(components.storage)
    real = engine._index_file

    async def index_file_then_compete(*args, **kwargs):
        result = await real(*args, **kwargs)
        await owner.start()
        return result

    monkeypatch.setattr(engine, "_index_file", index_file_then_compete)

    await _overlap(
        components.storage,
        owner,
        engine.index_file(path),
        while_queued=lambda: _relink(path, other),
    )

    assert _reason_under(components.storage, a_key) is None
    assert _reason_under(components.storage, b_key) == "source_missing"


def _contents_under(storage, key: str) -> list[tuple[str, str]]:
    return (
        storage._get_db()
        .execute("SELECT content, scope FROM chunks WHERE source_file=? ORDER BY id", (key,))
        .fetchall()
    )


async def test_an_emptied_file_relinked_during_the_scope_wait_stops_and_deletes_nothing(
    components, engine, memory_dir, tmp_path
):
    _symlink_or_skip(tmp_path)
    path, other = memory_dir / "a.md", memory_dir / "b.md"
    await _indexed(engine, path)
    await _indexed(engine, other, _OTHER)
    a_key, b_key = norm_path(path), norm_path(other)
    a_before = _contents_under(components.storage, a_key)
    b_before = _contents_under(components.storage, b_key)
    path.write_text("", encoding="utf-8")
    owner = _Owner(components.storage)
    await owner.start()

    with pytest.raises(RetryableError, match="names another file"):
        await _overlap(
            components.storage,
            owner,
            engine.index_file(path),
            while_queued=lambda: _relink(path, other),
        )

    assert _contents_under(components.storage, a_key) == a_before
    assert _contents_under(components.storage, b_key) == b_before


async def test_a_forced_reindex_relinked_during_the_scope_wait_leaves_the_target_alone(
    bm25_only_components, tmp_path
):
    """The review's case: A's content must not replace B's project rows as user rows."""
    _symlink_or_skip(tmp_path)
    comp, mem_dir = bm25_only_components
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    local = (root / ".memtomem" / "memories.local").resolve()
    local.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [local]
    other = local / "b.md"
    other.write_text(_OTHER, encoding="utf-8")
    assert not (await comp.index_engine.index_file(other)).errors
    path = mem_dir / "a.md"
    path.write_text(_NOTE, encoding="utf-8")
    assert not (await comp.index_engine.index_file(path)).errors
    a_key, b_key = norm_path(path), norm_path(other)
    a_before = _contents_under(comp.storage, a_key)
    b_before = _contents_under(comp.storage, b_key)
    path.write_text(_NOTE + "\n## Later\n\nedited falcon line\n", encoding="utf-8")
    owner = _Owner(comp.storage)
    await owner.start()

    with pytest.raises(RetryableError, match="names another file"):
        await _overlap(
            comp.storage,
            owner,
            comp.index_engine.index_file(path, force=True),
            while_queued=lambda: _relink(path, other),
        )

    assert _contents_under(comp.storage, b_key) == b_before
    assert _contents_under(comp.storage, a_key) == a_before


async def test_a_release_kept_busy_by_another_process_is_deferred(
    components, engine, memory_dir, monkeypatch, caplog
):
    """Another process's write lock makes ``BEGIN IMMEDIATE`` fail inside ``transaction()``.

    That arrives as a ``StorageError`` caused by "database is locked". The index
    succeeded, so the release is deferred, as a bare busy error already was.
    """
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    assert await components.storage.hold_source(path, "source_missing")
    storage = components.storage
    storage._get_db().execute("PRAGMA busy_timeout=50")
    other = sqlite3.connect(str(storage._config.sqlite_path), isolation_level=None)
    real = engine._index_file

    async def index_file_then_lock(*args, **kwargs):
        result = await real(*args, **kwargs)
        other.execute("BEGIN IMMEDIATE")
        return result

    monkeypatch.setattr(engine, "_index_file", index_file_then_lock)
    try:
        with caplog.at_level(logging.WARNING, logger="memtomem.indexing.engine"):
            stats = await engine.index_file(path)
    finally:
        if other.in_transaction:
            other.execute("ROLLBACK")
        other.close()

    assert not stats.errors
    assert _hold_reason(storage, path) == "source_missing"
    assert "deferred hold release" in caplog.text


async def test_a_release_failing_otherwise_still_raises(
    components, engine, memory_dir, monkeypatch
):
    """Only the busy shape is deferred; any other storage failure propagates."""
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    assert await components.storage.hold_source(path, "source_missing")

    async def broken(*args, **kwargs):
        raise StorageError("disk I/O error")

    monkeypatch.setattr(components.storage, "release_source_hold", broken)

    with pytest.raises(StorageError, match="disk I/O error"):
        await engine.index_file(path)


class _CompetingLLM:
    """An LLM whose answer arrives after another task has taken the writer."""

    def __init__(self, owner: _Owner, answer: str | None) -> None:
        self._owner, self._answer = owner, answer
        self.calls = 0

    async def generate(self, prompt: str, **_kwargs) -> str | None:
        self.calls += 1
        if self._owner.task is None:
            await self._owner.start()
        return self._answer


def _byte_tokenizer(tmp_path: Path) -> str:
    """A tiny byte-level tokenizer file, as ``test_bounded_chunks`` builds one."""
    tokenizers = pytest.importorskip("tokenizers")
    alphabet = sorted(tokenizers.pre_tokenizers.ByteLevel.alphabet())
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.BPE(vocab={c: i for i, c in enumerate(alphabet)}, merges=[])
    )
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.ByteLevel(add_prefix_space=False)
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    return str(path)


async def test_enrichment_waits_to_cache_its_descriptions(components, engine, memory_dir, tmp_path):
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    owner = _Owner(components.storage)
    engine._llm = _CompetingLLM(owner, "Describes the falcon plans.")
    engine._config.chunk_tokenizer_path = _byte_tokenizer(tmp_path)
    engine._config.hard_max_chunk_tokens = 64
    engine._config.chunk_context_tokens = 256
    engine._config.chunk_model_tokens = 384
    engine._config.enrich_chunk_context = True
    path.write_text("# Note\n\n## Plans\n\nbeta heron body\n", encoding="utf-8")

    stats = await _overlap(components.storage, owner, engine.index_file(path))

    assert not stats.errors
    assert await components.storage.get_chunk_descriptions(path)


async def test_enrichment_relinked_during_its_cache_wait_stops_before_the_diff(
    components, engine, memory_dir, tmp_path
):
    """The description cache waits after the LLM; the diff and chunk write do not follow a relink."""
    _symlink_or_skip(tmp_path)
    path, other = memory_dir / "a.md", memory_dir / "b.md"
    await _indexed(engine, path)
    await _indexed(engine, other, _OTHER)
    b_key = norm_path(other)
    b_before = _contents_under(components.storage, b_key)
    owner = _Owner(components.storage)
    engine._llm = _CompetingLLM(owner, "Describes the falcon plans.")
    engine._config.chunk_tokenizer_path = _byte_tokenizer(tmp_path)
    engine._config.hard_max_chunk_tokens = 64
    engine._config.chunk_context_tokens = 256
    engine._config.chunk_model_tokens = 384
    engine._config.enrich_chunk_context = True
    path.write_text("# Note\n\n## Plans\n\nbeta heron body\n", encoding="utf-8")

    with pytest.raises(RetryableError, match="names another file"):
        await _overlap(
            components.storage,
            owner,
            engine.index_file(path),
            while_queued=lambda: _relink(path, other),
        )

    assert _contents_under(components.storage, b_key) == b_before


async def test_a_summary_waits_to_be_written(components, engine, memory_dir):
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    owner = _Owner(components.storage)
    engine._llm = _CompetingLLM(owner, "A note about falcon plans.")
    engine._config.auto_summarize = True
    path.write_text("# Note\n\n## Plans\n\nbeta heron body\n", encoding="utf-8")

    stats = await _overlap(components.storage, owner, engine.index_file(path))

    assert not stats.errors
    record = await components.storage.get_ai_summary(path)
    assert record is not None and record["summary"] == "A note about falcon plans."


async def test_a_stale_summary_waits_to_be_dropped(components, engine, memory_dir):
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    await components.storage.set_ai_summary(
        path, summary="old falcon prose", signature="stale", language="en"
    )
    owner = _Owner(components.storage)
    engine._llm = _CompetingLLM(owner, None)
    engine._config.auto_summarize = True
    path.write_text("# Note\n\n## Plans\n\nbeta heron body\n", encoding="utf-8")

    stats = await _overlap(components.storage, owner, engine.index_file(path))

    assert not stats.errors
    assert await components.storage.get_ai_summary(path) is None


async def test_the_overlap_harness_rejects_a_call_refused_at_once(tmp_path: Path):
    """The harness's own control: on a refusal it reports that error, not a pass."""
    from memtomem.config import StorageConfig
    from memtomem.storage.sqlite_backend import SqliteBackend

    storage = SqliteBackend(StorageConfig(sqlite_path=str(tmp_path / "h.db")), dimension=4)
    await storage.initialize()
    try:
        owner = _Owner(storage)
        await owner.start()

        async def refused() -> None:
            storage._get_db()

        with pytest.raises(TransactionOwnedError):
            await _overlap(storage, owner, refused())
    finally:
        await storage.close()
