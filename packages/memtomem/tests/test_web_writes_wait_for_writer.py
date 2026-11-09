"""Writes that follow an ``await`` wait for another task's transaction (#3181).

``_get_db()`` refuses at once while another task owns ``storage.transaction()``.
At the sites below a refusal came after something durable had been written, so
it left the store in a wrong state rather than only answering 500:

- a scratch promote appended the entry but did not mark it promoted;
- an add with tags was indexed without its tags;
- the memory-dir remove sweep stopped partway, after the config was saved;
- an index-only chunk delete left the row searchable;
- auto-tag stopped partway;
- the watcher left a relinked leaf's rows visible.

Each write now waits in ``transaction(wait=WRITER_WAIT_S)``. The success tests
start a competing owner at the suspension point just before the write, check
the write is queued on the writer lock, release the owner, and check the
outcome. The timeout tests keep the owner past a shortened ``WRITER_WAIT_S``
and check the refusal states what was left behind. The owner enters the store's
own ``transaction``, captured before any test patches it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import memtomem.tools.auto_tag as auto_tag_mod
import memtomem.web.routes.scratch as scratch_mod
import memtomem.web.routes.system_config as system_config_mod
import memtomem.web.routes.system_indexing as system_indexing_mod
from memtomem.context._atomic import _intra_async_lock_for, memory_lock_path
from memtomem.errors import TransactionOwnedError
from memtomem.indexing.watcher import FileWatcher
from memtomem.storage.source_holds import RELINKED_HOLD
from memtomem.storage.sqlite_helpers import norm_path
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured

from .helpers import isolate_config_paths

_NOTE = "# Note\n\n## Plans\n\nalpha falcon body\n"
_SHORT_WAIT_S = 1.0  # long enough that a slow CI loop still sees the waiter
# The ingress guard scans the entry, not its title, so the index refuses this one.
_SECRET_TITLE = "aws " + "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path_factory) -> Path:
    return isolate_config_paths(monkeypatch, tmp_path_factory.mktemp("memtomem-home"))


class _Owner:
    """Another task that owns the writer until :meth:`finish`."""

    def __init__(self, storage, *, then=None) -> None:
        self._transaction = storage.transaction
        self._then = then
        self.entered, self._release = asyncio.Event(), asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        async with self._transaction():
            self.entered.set()
            await self._release.wait()
            if self._then is not None:
                await self._then()

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


async def _until_queued(storage, owner: _Owner, task: asyncio.Future) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not (owner.entered.is_set() and _queued(storage)) and not task.done():
        if loop.time() > deadline:
            break
        await asyncio.sleep(0.005)
    return owner.entered.is_set() and _queued(storage) and not task.done()


async def _overlap(storage, owner: _Owner, call, *, while_queued=None):
    """Run ``call`` while ``owner`` holds (or will take) the writer; return its result.

    The call must end up queued on the writer lock while the owner holds it.
    ``while_queued`` runs then, before the owner is released.
    """
    task = asyncio.ensure_future(call)
    queued = await _until_queued(storage, owner, task)
    witness = while_queued() if queued and while_queued is not None else None
    await owner.finish()
    result = await asyncio.wait_for(task, 10)
    assert queued, "the call never queued on the writer while the owner held it"
    return result, witness


async def _timed_out(storage, owner: _Owner, call):
    """Run ``call`` while ``owner`` holds the writer past the shortened wait."""
    task = asyncio.ensure_future(call)
    try:
        assert await _until_queued(storage, owner, task), "refused without waiting"
        return await asyncio.wait_for(task, 10)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await owner.finish()


def _client(comp) -> AsyncClient:
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.state.project_root = None
    app.state.csrf_enforce = False
    app.dependency_overrides[require_configured] = lambda: None
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    )


def _start_owner_after(monkeypatch, obj, name: str, owner: _Owner, *, before=None) -> None:
    """Start ``owner`` once ``obj.name`` returns (or raises), the first time only.

    ``before`` runs first, synchronously, to set up the state the site will read.
    """
    real = getattr(obj, name)

    async def wrapped(*args, **kwargs):
        try:
            return await real(*args, **kwargs)
        finally:
            if owner.task is None:
                if before is not None:
                    before()
                await owner.start()

    monkeypatch.setattr(obj, name, wrapped)


async def _indexed(engine, path: Path, text: str = _NOTE) -> None:
    path.write_text(text, encoding="utf-8")
    stats = await engine.index_file(path)
    assert not stats.errors, stats.errors
    assert stats.indexed_chunks > 0


# ---- scratch promote ---------------------------------------------------------


async def _scratch(storage, key: str = "task", value: str = "promote me body") -> str:
    await storage.scratch_set(key, value)
    return key


async def test_promote_marks_the_entry_once_the_owner_finishes(components, monkeypatch):
    storage = components.storage
    key = await _scratch(storage)
    owner = _Owner(storage)
    _start_owner_after(monkeypatch, components.index_engine, "index_file", owner)

    async with _client(components) as client:
        resp, sidecar_held = await _overlap(
            storage,
            owner,
            client.post(f"/api/scratch/{key}/promote", json={}),
            # The mark waits under the sidecar: released first, a second
            # promote could read the entry unmarked and append it again.
            while_queued=lambda: any(
                _intra_async_lock_for(memory_lock_path(p)).locked()
                for p in Path(components.config.indexing.memory_dirs[0]).glob("*.md")
            ),
        )

    assert resp.status_code == 200, resp.text
    assert sidecar_held, "the mark waited after the sidecar was released"
    assert (await storage.scratch_get(key))["promoted"]


async def test_a_timed_out_mark_says_the_entry_was_appended(components, monkeypatch):
    storage = components.storage
    key = await _scratch(storage)
    monkeypatch.setattr(scratch_mod, "WRITER_WAIT_S", _SHORT_WAIT_S)
    owner = _Owner(storage)
    _start_owner_after(monkeypatch, components.index_engine, "index_file", owner)

    async with _client(components) as client:
        resp = await _timed_out(storage, owner, client.post(f"/api/scratch/{key}/promote", json={}))

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert "appended" in detail and "not be marked promoted" in detail
    assert "delete it from working memory" in detail
    assert not (await storage.scratch_get(key))["promoted"]
    files = list(Path(components.config.indexing.memory_dirs[0]).glob("*.md"))
    assert sum(f.read_text(encoding="utf-8").count("promote me body") for f in files) == 1


async def test_a_timed_out_mark_after_a_privacy_refusal_keeps_its_diagnosis(
    components, monkeypatch
):
    """The privacy arm marks too; when that times out, the refusal says both things."""
    storage = components.storage
    key = await _scratch(storage)
    monkeypatch.setattr(scratch_mod, "WRITER_WAIT_S", _SHORT_WAIT_S)
    owner = _Owner(storage)
    _start_owner_after(monkeypatch, components.index_engine, "index_file", owner)
    async with _client(components) as client:
        resp = await _timed_out(
            storage,
            owner,
            client.post(f"/api/scratch/{key}/promote", json={"title": _SECRET_TITLE}),
        )

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert "refused" in detail  # the privacy diagnosis, first
    assert "not be marked promoted" in detail
    assert not (await storage.scratch_get(key))["promoted"]


async def test_a_privacy_refusal_still_marks_once_the_owner_finishes(components, monkeypatch):
    """The privacy arm's mark waits too, and the caller still gets the privacy 409."""
    storage = components.storage
    key = await _scratch(storage)
    owner = _Owner(storage)
    _start_owner_after(monkeypatch, components.index_engine, "index_file", owner)

    async with _client(components) as client:
        resp, _ = await _overlap(
            storage,
            owner,
            client.post(f"/api/scratch/{key}/promote", json={"title": _SECRET_TITLE}),
        )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["detail"] == "index_blocked"
    assert (await storage.scratch_get(key))["promoted"]


async def test_a_timed_out_mark_after_an_exclusion_keeps_the_notice(components, monkeypatch):
    """The file became excluded during the append: the 503 says so before the mark."""
    from memtomem.source_provenance import EXCLUDED_AFTER_APPEND_NOTICE

    storage = components.storage
    key = await _scratch(storage)
    monkeypatch.setattr(scratch_mod, "WRITER_WAIT_S", _SHORT_WAIT_S)
    owner = _Owner(storage)
    real_index = components.index_engine.index_file

    async def excluded_then_start(*args, **kwargs):
        stats = await real_index(*args, **kwargs)
        await owner.start()
        return dataclasses.replace(stats, excluded_files=1)

    monkeypatch.setattr(components.index_engine, "index_file", excluded_then_start)

    async with _client(components) as client:
        resp = await _timed_out(storage, owner, client.post(f"/api/scratch/{key}/promote", json={}))

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail.startswith(EXCLUDED_AFTER_APPEND_NOTICE)
    assert "not be marked promoted" in detail


# ---- add with tags -----------------------------------------------------------


async def _chunks_with(storage, path: Path, needle: str):
    rows = await storage.list_chunks_by_source(path, limit=None)
    return [c for c in rows if needle in c.content]


def _strip_tags(storage, needle: str):
    """Leave the new entry's chunks without tags, so the route's merge has work to do.

    The appended entry carries its tags and the index usually stores them; the
    merge only writes when it did not.
    """

    def strip() -> None:
        db = storage._get_db()
        db.execute("UPDATE chunks SET tags = '[]' WHERE content LIKE ?", (f"%{needle}%",))
        db.commit()

    return strip


async def test_add_whose_index_stored_the_tags_takes_no_writer(components, memory_dir, monkeypatch):
    """Nothing to merge, nothing to wait for: an owner held throughout does not matter."""
    storage = components.storage
    target = memory_dir / "notes.md"
    owner = _Owner(storage)
    _start_owner_after(monkeypatch, components.index_engine, "index_file", owner)
    monkeypatch.setattr(system_indexing_mod, "WRITER_WAIT_S", _SHORT_WAIT_S)

    async with _client(components) as client:
        try:
            resp = await client.post(
                "/api/add",
                json={"content": "heron entry body", "file": target.name, "tags": ["mine"]},
            )
        finally:
            await owner.finish()

    assert resp.status_code == 200, resp.text
    [chunk] = await _chunks_with(storage, target, "heron entry")
    assert "mine" in chunk.metadata.tags


async def test_add_tags_merge_with_what_the_owner_committed(components, memory_dir, monkeypatch):
    """The read happens after the wait: a tag the owner wrote is merged, not overwritten."""
    storage = components.storage
    target = memory_dir / "notes.md"

    async def owner_tags() -> None:
        for c in await _chunks_with(storage, target, "heron entry"):
            await storage.set_chunk_tags(c.id, ["by-owner"])

    owner = _Owner(storage, then=owner_tags)
    _start_owner_after(
        monkeypatch,
        components.index_engine,
        "index_file",
        owner,
        before=_strip_tags(storage, "heron entry"),
    )

    async with _client(components) as client:
        resp, _ = await _overlap(
            storage,
            owner,
            client.post(
                "/api/add",
                json={"content": "heron entry body", "file": target.name, "tags": ["mine"]},
            ),
        )

    assert resp.status_code == 200, resp.text
    [chunk] = await _chunks_with(storage, target, "heron entry")
    assert set(chunk.metadata.tags) == {"by-owner", "mine"}


async def test_a_timed_out_tag_merge_says_the_entry_was_added(components, memory_dir, monkeypatch):
    storage = components.storage
    target = memory_dir / "notes.md"
    monkeypatch.setattr(system_indexing_mod, "WRITER_WAIT_S", _SHORT_WAIT_S)
    owner = _Owner(storage)
    _start_owner_after(
        monkeypatch,
        components.index_engine,
        "index_file",
        owner,
        before=_strip_tags(storage, "heron entry"),
    )

    async with _client(components) as client:
        resp = await _timed_out(
            storage,
            owner,
            client.post(
                "/api/add",
                json={"content": "heron entry body", "file": target.name, "tags": ["mine"]},
            ),
        )

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert "added to notes.md and indexed" in detail and "tags were not applied" in detail
    [chunk] = await _chunks_with(storage, target, "heron entry")
    assert "mine" not in chunk.metadata.tags
    assert target.read_text(encoding="utf-8").count("heron entry body") == 1


# ---- memory-dir remove sweep -------------------------------------------------


async def _two_dirs(components, tmp_path: Path) -> tuple[Path, Path]:
    keep = Path(components.config.indexing.memory_dirs[0]).expanduser().resolve()
    going = (tmp_path / "going").resolve()
    going.mkdir()
    components.config.indexing.memory_dirs = [keep, going]
    for name in ("a.md", "b.md"):
        await _indexed(components.index_engine, going / name)
    return keep, going


def _remove(client: AsyncClient, going: Path):
    return client.post("/api/memory-dirs/remove", json={"path": str(going), "delete_chunks": True})


async def _source_rows(storage, going: Path) -> int:
    rows = await storage.get_source_files_with_counts()
    return sum(1 for r in rows if str(r[0]).startswith(norm_path(going)))


@pytest.fixture
def _no_save():
    """Keep the route's config save and disk reload away from the isolated home.

    The reload would rebuild the config from disk and drop the second root this
    test registered in memory.
    """
    with (
        patch("memtomem.web.routes.system.save_config_overrides"),
        patch("memtomem.web.hot_reload.reload_if_stale", AsyncMock(return_value=None)),
    ):
        yield


async def test_remove_sweep_runs_once_the_owner_finishes(components, tmp_path, _no_save):
    storage = components.storage
    _, going = await _two_dirs(components, tmp_path)
    owner = _Owner(storage)
    await owner.start()

    async with _client(components) as client:
        resp, _ = await _overlap(storage, owner, _remove(client, going))

    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted_chunks"] > 0
    assert await _source_rows(storage, going) == 0


async def test_a_timed_out_sweep_deletes_nothing_and_still_reconfigures(
    components, tmp_path, monkeypatch, _no_save
):
    storage = components.storage
    _, going = await _two_dirs(components, tmp_path)
    monkeypatch.setattr(system_config_mod, "WRITER_WAIT_S", _SHORT_WAIT_S)
    owner = _Owner(storage)
    await owner.start()

    async with _client(components) as client:
        watcher = MagicMock(reconfigure=AsyncMock())
        client._transport.app.state.file_watcher = watcher
        resp = await _timed_out(storage, owner, _remove(client, going))

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert "was removed from memory_dirs" in detail and "none of its chunks" in detail
    assert "Sources page" in detail and "retry" not in detail.lower()
    assert await _source_rows(storage, going) == 2
    watcher.reconfigure.assert_awaited_once()
    assert going not in components.config.indexing.memory_dirs


async def test_a_watcher_failure_after_a_timed_out_sweep_is_reported_not_raised(
    components, tmp_path, monkeypatch, _no_save
):
    storage = components.storage
    _, going = await _two_dirs(components, tmp_path)
    monkeypatch.setattr(system_config_mod, "WRITER_WAIT_S", _SHORT_WAIT_S)
    owner = _Owner(storage)
    await owner.start()

    async with _client(components) as client:
        client._transport.app.state.file_watcher = MagicMock(
            reconfigure=AsyncMock(side_effect=RuntimeError("watch blip"))
        )
        resp = await _timed_out(storage, owner, _remove(client, going))

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert "none of its chunks" in detail and "file watcher could not be updated" in detail


class _ShortDeadline:
    """``asyncio`` as the remove route sees it, with its outer deadline cut short."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds

    def timeout(self, _delay):
        return asyncio.timeout(self._seconds)

    def __getattr__(self, name):
        return getattr(asyncio, name)


async def test_the_sweep_wait_does_not_inherit_what_the_lock_wait_spent(
    components, tmp_path, monkeypatch, _no_save
):
    """The config lock took most of the deadline; the sweep still answers for the save.

    The writer wait after the save gets a budget of its own, so it runs out as a
    refusal that names the saved state and updates the watcher, not as the
    request's deadline.
    """
    from memtomem.web.routes._locks import _config_lock

    storage = components.storage
    _, going = await _two_dirs(components, tmp_path)
    monkeypatch.setattr(system_config_mod, "WRITER_WAIT_S", _SHORT_WAIT_S)
    monkeypatch.setattr(system_config_mod, "_asyncio", _ShortDeadline(1.5))
    owner = _Owner(storage)
    await owner.start()

    async with _client(components) as client:
        watcher = MagicMock(reconfigure=AsyncMock())
        client._transport.app.state.file_watcher = watcher
        release = asyncio.Event()

        async def hold_config_lock() -> None:
            async with _config_lock:
                await release.wait()

        holder = asyncio.create_task(hold_config_lock())
        while not _config_lock.locked():
            await asyncio.sleep(0.005)
        task = asyncio.ensure_future(_remove(client, going))
        try:
            await asyncio.sleep(1.2)  # most of the 1.5 s deadline, spent waiting for the lock
        finally:
            release.set()
            await holder
        resp = await _timed_out(storage, owner, task)

    assert resp.status_code == 503, resp.text
    assert "none of its chunks" in resp.json()["detail"]
    watcher.reconfigure.assert_awaited_once()
    assert await _source_rows(storage, going) == 2


async def test_a_deadline_after_the_save_names_the_saved_state(
    components, tmp_path, monkeypatch, _no_save
):
    """Should the post-save budget run out too, the 503 says the directory is gone."""
    _, going = await _two_dirs(components, tmp_path)
    monkeypatch.setattr(system_config_mod, "_POST_SAVE_BUDGET_S", 0.2)

    async def slow_reconfigure(_config) -> None:
        await asyncio.sleep(5)

    async with _client(components) as client:
        client._transport.app.state.file_watcher = MagicMock(reconfigure=slow_reconfigure)
        resp = await _remove(client, going)

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert "was removed from memory_dirs" in detail and "did not finish in time" in detail
    assert going not in components.config.indexing.memory_dirs


async def test_a_sweep_that_fails_partway_deletes_nothing(
    components, tmp_path, monkeypatch, _no_save
):
    """One transaction: a failure on the second source rolls the first one back too."""
    storage = components.storage
    _, going = await _two_dirs(components, tmp_path)
    real = storage.delete_by_source
    calls = []

    async def second_fails(path, **kwargs):
        calls.append(path)
        if len(calls) == 2:
            raise RuntimeError("store blip")
        return await real(path, **kwargs)

    monkeypatch.setattr(storage, "delete_by_source", second_fails)

    async with _client(components) as client:
        resp = await _remove(client, going)

    assert resp.status_code == 500
    assert len(calls) == 2
    assert await _source_rows(storage, going) == 2


# ---- index-only chunk delete -------------------------------------------------


async def _index_only_delete(components, memory_dir, monkeypatch, owner: _Owner):
    """A delete whose source vanishes mid-write, with ``owner`` started right before the row
    delete; returns ``(chunk, path, request)``."""
    from memtomem.web.routes import chunks as chunks_route

    storage = components.storage
    path = memory_dir / "note.md"
    await _indexed(components.index_engine, path)
    [chunk] = await storage.list_chunks_by_source(path, limit=None)
    armed = {"on": False}
    real_remove = chunks_route.remove_lines

    def remove_after_unlink(p, *args, **kwargs):
        p.unlink()
        armed["on"] = True
        return real_remove(p, *args, **kwargs)

    monkeypatch.setattr(chunks_route, "remove_lines", remove_after_unlink)
    real_get = storage.get_chunk

    async def get_then_start(*args, **kwargs):
        result = await real_get(*args, **kwargs)
        if armed["on"] and owner.task is None:
            await owner.start()
        return result

    monkeypatch.setattr(storage, "get_chunk", get_then_start)
    return chunk, path, real_get


async def test_index_only_delete_waits_and_finalizes_the_emptied_source(
    components, memory_dir, monkeypatch
):
    """The source vanished mid-delete; the row delete waits and runs its standalone cleanup."""
    storage = components.storage
    owner = _Owner(storage)
    chunk, path, real_get = await _index_only_delete(components, memory_dir, monkeypatch, owner)
    db = storage._get_db()
    summary_key = f"ai_summary:{norm_path(path)}"
    db.execute(
        "INSERT OR REPLACE INTO _memtomem_meta (key, value) VALUES (?, ?)",
        (summary_key, "a summary"),
    )
    db.commit()

    async with _client(components) as client:
        resp, _ = await _overlap(storage, owner, client.delete(f"/api/chunks/{chunk.id}"))

    assert resp.status_code == 200, resp.text
    assert await real_get(chunk.id) is None
    row = db.execute("SELECT 1 FROM _memtomem_meta WHERE key = ?", (summary_key,)).fetchone()
    assert row is None, "the emptied source's summary survived the delete"


async def test_a_timed_out_index_only_delete_keeps_its_failure_detail(
    components, memory_dir, monkeypatch
):
    """The existing 500 already states the post-condition; the row is still there."""
    from memtomem.web.routes import chunks as chunks_route

    storage = components.storage
    monkeypatch.setattr(chunks_route, "WRITER_WAIT_S", _SHORT_WAIT_S)
    owner = _Owner(storage)
    chunk, _, real_get = await _index_only_delete(components, memory_dir, monkeypatch, owner)

    async with _client(components) as client:
        resp = await _timed_out(storage, owner, client.delete(f"/api/chunks/{chunk.id}"))

    assert resp.status_code == 500, resp.text
    assert "no source file was changed" in resp.json()["detail"]
    assert await real_get(chunk.id) is not None


# ---- auto-tag ----------------------------------------------------------------


async def test_auto_tag_writes_once_the_owner_finishes(components, memory_dir, monkeypatch):
    storage = components.storage
    path = memory_dir / "note.md"
    await _indexed(components.index_engine, path)
    owner = _Owner(storage)

    async def extract(*_args, **_kwargs):
        if owner.task is None:
            await owner.start()
        return ["falcon"]

    monkeypatch.setattr(auto_tag_mod, "_extract_tags_with_fallback", extract)

    stats, _ = await _overlap(storage, owner, auto_tag_mod.auto_tag_storage(storage))

    assert stats.tagged_chunks > 0
    for c in await storage.list_chunks_by_source(path, limit=None):
        assert "falcon" in c.metadata.tags


async def test_auto_tag_waits_for_the_writer_before_the_tag_lock(
    components, memory_dir, monkeypatch
):
    """An index holds its transaction, then takes the tag lock.

    Were auto-tag to hold the tag lock while it waits for the writer, each would
    wait for the other until the writer wait ran out.
    """
    storage = components.storage
    path = memory_dir / "note.md"
    await _indexed(components.index_engine, path)

    async def take_tag_lock() -> None:
        async with asyncio.timeout(2), storage._tag_write_lock:
            pass

    owner = _Owner(storage, then=take_tag_lock)

    async def extract(*_args, **_kwargs):
        if owner.task is None:
            await owner.start()
        return ["falcon"]

    monkeypatch.setattr(auto_tag_mod, "_extract_tags_with_fallback", extract)

    stats, _ = await _overlap(storage, owner, auto_tag_mod.auto_tag_storage(storage))

    assert stats.tagged_chunks > 0


async def test_a_timed_out_auto_tag_write_raises_after_the_wait(
    components, memory_dir, monkeypatch
):
    """Past the wait the run stops with the refusal; the chunk keeps the tags it had."""
    storage = components.storage
    path = memory_dir / "note.md"
    await _indexed(components.index_engine, path)
    monkeypatch.setattr(auto_tag_mod, "WRITER_WAIT_S", _SHORT_WAIT_S)
    owner = _Owner(storage)

    async def extract(*_args, **_kwargs):
        if owner.task is None:
            await owner.start()
        return ["falcon"]

    monkeypatch.setattr(auto_tag_mod, "_extract_tags_with_fallback", extract)

    with pytest.raises(TransactionOwnedError, match="timed out"):
        await _timed_out(storage, owner, auto_tag_mod.auto_tag_storage(storage))

    for c in await storage.list_chunks_by_source(path, limit=None):
        assert "falcon" not in c.metadata.tags


async def test_auto_tag_route_reports_a_partial_run():
    app = create_app(lifespan=None, mode="dev")
    app.state.storage = AsyncMock()
    app.state.search_pipeline = MagicMock()
    app.state.csrf_enforce = False
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        with patch(
            "memtomem.web.routes.tags.auto_tag_storage",
            AsyncMock(side_effect=TransactionOwnedError("timed out waiting for writer lock")),
        ):
            resp = await client.post("/api/tags/auto", json={"dry_run": False})

    assert resp.status_code == 503, resp.text
    assert "stopped partway" in resp.json()["detail"]


# ---- watcher relinked leaf ---------------------------------------------------


async def _relinked(components, memory_dir) -> tuple[Path, str]:
    """``note.md`` indexed, then replaced by a link to ``other.md``; returns it and its key."""
    path = memory_dir / "note.md"
    other = memory_dir / "other.md"
    await _indexed(components.index_engine, path)
    await _indexed(components.index_engine, other, "# Other\n\nbeta heron body\n")
    key = norm_path(path)  # the leaf's stored key, taken while it is a regular file
    path.unlink()
    path.symlink_to(other)
    return path, key


def _hold_reason(storage, key: str) -> str | None:
    row = (
        storage._get_db()
        .execute("SELECT reason FROM held_sources WHERE source_file = ?", (key,))
        .fetchone()
    )
    return None if row is None else row[0]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
async def test_relinked_leaf_hold_waits_out_another_tasks_transaction(components, memory_dir):
    storage = components.storage
    path, key = await _relinked(components, memory_dir)
    watcher = FileWatcher(components.index_engine, components.config.indexing)
    owner = _Owner(storage)
    await owner.start()

    settled, _ = await _overlap(storage, owner, watcher._hold_relinked_leaf(path))

    assert settled is True
    assert _hold_reason(storage, key) == RELINKED_HOLD


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
async def test_a_timed_out_relinked_hold_asks_for_a_retry(components, memory_dir, monkeypatch):
    """Past the wait nothing is held and the watcher is told to retry the path."""
    import memtomem.indexing.watcher as watcher_mod

    storage = components.storage
    path, key = await _relinked(components, memory_dir)
    monkeypatch.setattr(watcher_mod, "WRITER_WAIT_S", _SHORT_WAIT_S)
    watcher = FileWatcher(components.index_engine, components.config.indexing)
    owner = _Owner(storage)
    await owner.start()

    settled = await _timed_out(storage, owner, watcher._hold_relinked_leaf(path))

    assert settled is False
    assert _hold_reason(storage, key) is None
