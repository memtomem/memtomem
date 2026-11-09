"""A by-id rewrite's preflight and the bulk summary regenerate wait for the writer (#3179).

``_get_db()`` refuses at once while another task owns ``storage.transaction()``.
#3173 made the single-file index path wait; two writer accesses outside it
still followed an ``await`` without waiting: the stored-scope read in
``IndexEngine.rewrite_refusal`` (the preflight of a by-id edit or delete) and
the summary write in ``regenerate_for_paths`` (after its LLM call).

The tests of a successful wait install the competing owner at the suspension
point just before the site, check the call is queued on the writer lock,
release the owner, and check the outcome. The timeout tests keep the owner past
a shortened ``WRITER_WAIT_S`` and check the refusal instead. The owner uses the
store's own ``transaction``, captured before any test patches it.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import memtomem.indexing.engine as engine_mod
import memtomem.storage.sqlite_helpers as helpers_mod
from memtomem.errors import TransactionOwnedError
from memtomem.indexing.summarizer import regenerate_for_paths
from memtomem.storage.sqlite_helpers import norm_path

_NOTE = "# Note\n\n## Plans\n\nalpha falcon body\n"


class _Owner:
    """Another task that owns the writer until :meth:`finish`."""

    def __init__(self, storage) -> None:
        self._transaction = storage.transaction
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
    ``while_queued`` runs then, before the owner is released. The owner is
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


async def _indexed(engine, path: Path, text: str = _NOTE) -> None:
    path.write_text(text, encoding="utf-8")
    stats = await engine.index_file(path)
    assert not stats.errors, stats.errors
    assert stats.indexed_chunks > 0


# ---- rewrite_refusal ---------------------------------------------------------


async def test_rewrite_preflight_waits_out_another_tasks_transaction(components, memory_dir):
    """The measured failure: the preflight was refused before the edit wrote anything."""
    engine = components.index_engine
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    owner = _Owner(components.storage)
    await owner.start()

    refusal = await _overlap(
        components.storage, owner, engine.rewrite_refusal(norm_path(path), op="edit")
    )

    assert refusal is None


async def test_rewrite_preflight_still_refuses_a_scope_change_after_the_wait(
    components, memory_dir
):
    """The rows read after the wait still decide: project rows that would turn user refuse.

    The rows are stored as ``project_local`` while the path now classifies as
    ``user``, as when the project's tier was unregistered since (#2920).
    """
    engine = components.index_engine
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    db = components.storage._get_db()
    db.execute(
        "UPDATE chunks SET scope='project_local', project_root=? WHERE source_file=?",
        (str(memory_dir), norm_path(path)),
    )
    db.commit()
    owner = _Owner(components.storage)
    await owner.start()

    refusal = await _overlap(
        components.storage, owner, engine.rewrite_refusal(norm_path(path), op="edit")
    )

    assert refusal is not None
    assert "note.md" in refusal


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
async def test_a_key_relinked_during_the_wait_is_refused(components, memory_dir):
    """The relink check runs after the wait, so a relink made while waiting is seen."""
    engine = components.index_engine
    path = memory_dir / "note.md"
    other = memory_dir / "other.md"
    await _indexed(engine, path)
    await _indexed(engine, other, "# Other\n\nbeta heron body\n")
    key = norm_path(path)
    owner = _Owner(components.storage)
    await owner.start()

    def relink() -> None:
        path.unlink()
        path.symlink_to(other)

    refusal = await _overlap(
        components.storage,
        owner,
        engine.rewrite_refusal(key, op="edit"),
        while_queued=relink,
    )

    assert refusal is not None
    assert helpers_mod.stored_key_moved(key)


async def test_rewrite_preflight_times_out_as_a_refusal(components, memory_dir, monkeypatch):
    """A writer held past the budget still ends in ``TransactionOwnedError``."""
    engine = components.index_engine
    path = memory_dir / "note.md"
    await _indexed(engine, path)
    # Long enough that a slow CI loop still sees the waiter before it times out.
    monkeypatch.setattr(engine_mod, "WRITER_WAIT_S", 1.0)
    storage = components.storage
    owner = _Owner(storage)
    await owner.start()
    task = asyncio.ensure_future(engine.rewrite_refusal(norm_path(path), op="edit"))
    try:
        for _ in range(100):
            if _queued(storage) or task.done():
                break
            await asyncio.sleep(0.005)
        assert _queued(storage) and not task.done(), "refused without waiting"
        with pytest.raises(TransactionOwnedError, match="timed out"):
            await task
        assert not _queued(storage)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await owner.finish()


# ---- regenerate_for_paths ----------------------------------------------------


class _LLM:
    """Starts ``owner`` inside the LLM call, so the write after it meets the owner."""

    def __init__(self, owner: _Owner | None = None) -> None:
        self.owner = owner

    async def generate(self, user, *, system=None, max_tokens=None):
        if self.owner is not None and self.owner.task is None:
            await self.owner.start()
        return "A regenerated summary."


def _config(**over):
    base = dict(
        auto_summarize=True,
        summary_language="en",
        summary_max_tokens=64,
        summary_max_input_chars=4000,
    )
    base.update(over)
    return SimpleNamespace(**base)


async def _regen_setup(components, memory_dir):
    path = memory_dir / "note.md"
    await _indexed(components.index_engine, path)
    key = Path(norm_path(path))
    storage = components.storage

    async def chunks_for(p: Path):
        return await storage.list_chunks_by_source(p, limit=64, as_stored=True)

    return storage, key, chunks_for


async def test_bulk_regenerate_writes_once_the_owner_finishes(components, memory_dir):
    """Before: the write after the LLM call was refused and aborted the whole job."""
    storage, key, chunks_for = await _regen_setup(components, memory_dir)
    owner = _Owner(storage)

    result = await _overlap(
        storage,
        owner,
        regenerate_for_paths(storage, _LLM(owner), [key], chunks_for, _config()),
    )

    assert result == {"processed": 1, "skipped": 0, "failed": 0}
    cached = await storage.get_ai_summary(key, as_stored=True)
    assert cached is not None and cached["summary"] == "A regenerated summary."


async def test_a_timed_out_write_counts_as_failed_and_the_job_goes_on(
    components, memory_dir, monkeypatch, caplog
):
    """One path's wait running out does not abort the paths after it."""
    import hashlib
    import logging

    import memtomem.indexing.summarizer as summarizer_mod

    storage = components.storage
    # A name the timeout warning must not carry: paths can be secret-shaped.
    first, second = memory_dir / "zz-private-name.md", memory_dir / "b.md"
    await _indexed(components.index_engine, first)
    await _indexed(components.index_engine, second, "# B\n\nbeta heron body\n")
    keys = [Path(norm_path(first)), Path(norm_path(second))]
    await storage.set_ai_summary(
        keys[0], summary="old", signature="s", language="en", as_stored=True
    )
    monkeypatch.setattr(summarizer_mod, "WRITER_WAIT_S", 0.1)
    owner = _Owner(storage)
    calls = 0

    class _TwoCalls:
        async def generate(self, user, *, system=None, max_tokens=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                await owner.start()  # the first path's write meets it and times out
            else:
                await owner.finish()  # gone before the second path's write
            return f"summary {calls}"

    async def chunks_for(p: Path):
        return await storage.list_chunks_by_source(p, limit=64, as_stored=True)

    progress: list[tuple[int, int, int]] = []
    caplog.set_level(logging.WARNING, logger=summarizer_mod.__name__)
    try:
        result = await regenerate_for_paths(
            storage,
            _TwoCalls(),
            keys,
            chunks_for,
            _config(),
            progress=lambda *a: progress.append(a),
        )
    finally:
        await owner.finish()  # a refusal before the second call would leave it open

    assert result == {"processed": 1, "skipped": 0, "failed": 1}
    assert progress == [(0, 2, 1), (1, 2, 1)]
    assert (await storage.get_ai_summary(keys[0], as_stored=True))["summary"] == "old"
    assert (await storage.get_ai_summary(keys[1], as_stored=True))["summary"] == "summary 2"
    skipped = [r.getMessage() for r in caplog.records if "regenerate skipped" in r.getMessage()]
    assert len(skipped) == 1
    assert hashlib.sha256(str(keys[0]).encode()).hexdigest()[:12] in skipped[0]
    assert "zz-private-name" not in caplog.text
