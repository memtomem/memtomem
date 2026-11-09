"""Web routes wait for another task's write transaction (#3182).

While another task owns ``storage.transaction()``, ``_get_db()`` refuses at once
with ``TransactionOwnedError``, and ``_require_transaction_idle`` with a
``StorageError``. These routes reached the writer after an ``await`` without
waiting, so they answered the generic HTTP 500. They now wait up to
``WRITER_WAIT_S`` and, when the wait runs out, answer 503 with a detail that
states what happened.

Each success test installs the competing owner, sends the request, checks it is
queued on the writer lock, releases the owner, and checks the response and the
stored state. Each timeout test keeps the owner past a shortened
``WRITER_WAIT_S`` and checks the 503 and that nothing changed. These run
against a real ``SqliteBackend`` through the real app.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import memtomem.quality.state as quality_state_mod
import memtomem.services.namespace_management as ns_mgmt_mod
import memtomem.services.tag_management as tag_mgmt_mod
import memtomem.storage.mixins.eval_cases as eval_cases_mod
import memtomem.storage.mixins.history as history_mod
import memtomem.storage.mixins.scratch as scratch_mod
import memtomem.storage.mixins.sessions as sessions_mod
import memtomem.storage.sqlite_backend as backend_mod
import memtomem.web.routes._writer_wait as writer_wait_mod
from helpers import isolate_config_paths
from memtomem.errors import StorageError
from memtomem.web import hot_reload
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured
from memtomem.web.routes._writer_wait import (
    WRITER_BUSY_NOTHING_CHANGED_DETAIL,
    WRITER_BUSY_READ_DETAIL,
    WRITER_BUSY_REFUSED_NOTHING_CHANGED_DETAIL,
)

_NOTE = "## Alpha\n\nalpha falcon body\n\n## Beta\n\nbeta heron body\n"
_WAIT_MODULES = (
    backend_mod,
    eval_cases_mod,
    history_mod,
    scratch_mod,
    sessions_mod,
    tag_mgmt_mod,
    quality_state_mod,
    writer_wait_mod,
)


class _Owner:
    """Another task that owns the writer until :meth:`finish`.

    ``inside`` runs in the owner's transaction after the release, before the
    commit, so a test can change rows while a request waits.
    """

    def __init__(self, storage, inside=None) -> None:
        self._storage = storage
        self._transaction = storage.transaction
        self._inside = inside
        self.entered, self._release = asyncio.Event(), asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        async with self._transaction():
            self.entered.set()
            await self._release.wait()
            if self._inside is not None:
                await self._inside(self._storage)

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


async def _send_while_owned(env, method: str, url: str, *, inside=None, **kwargs):
    """Send a request while another task owns the writer; return ``(queued, response)``."""
    owner = _Owner(env.storage, inside)
    await owner.start()
    task = asyncio.ensure_future(env.client.request(method, url, **kwargs))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not _queued(env.storage) and not task.done() and loop.time() < deadline:
        await asyncio.sleep(0.005)
    queued = _queued(env.storage) and not task.done()
    await owner.finish()
    return queued, await asyncio.wait_for(task, 10)


async def _send_past_wait(env, method: str, url: str, **kwargs):
    """Send a request while another task keeps the writer past the wait."""
    owner = _Owner(env.storage)
    await owner.start()
    try:
        return await asyncio.wait_for(env.client.request(method, url, **kwargs), 10)
    finally:
        await owner.finish()


@pytest.fixture
def short_wait(monkeypatch):
    for mod in _WAIT_MODULES:
        monkeypatch.setattr(mod, "WRITER_WAIT_S", 0.05)


def _web_app(comp):
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.dependency_overrides[require_configured] = lambda: None
    app.state.config_signature = hot_reload.current_signature()
    app.state.last_reload_error = None
    return app


@pytest.fixture
async def env(bm25_only_components, tmp_path_factory, monkeypatch):
    comp, mem_dir = bm25_only_components
    isolate_config_paths(monkeypatch, tmp_path_factory.mktemp("config-home"))
    storage = comp.storage
    note = mem_dir / "note.md"
    note.write_text(_NOTE, encoding="utf-8")
    await comp.index_engine.index_file(note)
    chunks = await storage.list_chunks_by_source(note)
    for chunk in chunks:
        await storage.set_chunk_tags(chunk.id, ["t1"])
    await storage.scratch_set("k1", "v1")
    sid = str(uuid.uuid4())
    await storage.create_session(sid, "agent1", "default")
    app = _web_app(comp)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        resp = await client.get("/api/search", params={"q": "alpha"})
        run_id = resp.json()["query_run_id"]
        await comp.search_pipeline.flush_observation()
        detail = (await client.get(f"/api/search/runs/{run_id}")).json()
        yield SimpleNamespace(
            comp=comp,
            storage=storage,
            client=client,
            note=note,
            mem_dir=mem_dir,
            chunk_id=str(chunks[0].id),
            snapshot_chunk_id=detail["results"][0]["chunk_id"],
            sid=sid,
            run_id=run_id,
        )


async def _tags(env) -> list[list[str]]:
    return [list(c.metadata.tags) for c in await env.storage.list_chunks_by_source(env.note)]


# ---------------------------------------------------------------------------
# Reads


_READS = [
    ("GET", "/api/namespaces", {}),
    ("GET", "/api/namespaces/default", {}),
    ("GET", "/api/scratch", {}),
    ("GET", "/api/search/runs", {}),
    ("GET", "/api/search/runs/{run_id}", {}),
    ("GET", "/api/sessions", {}),
    ("GET", "/api/sessions/{sid}/events", {}),
    ("GET", "/api/embedding-status", {}),
    ("POST", "/api/quality/replay", {"json": {}}),
]


@pytest.mark.parametrize(("method", "url", "kwargs"), _READS)
async def test_a_read_waits_for_the_writer(env, method, url, kwargs):
    url = url.format(run_id=env.run_id, sid=env.sid)
    queued, resp = await _send_while_owned(env, method, url, **kwargs)
    assert queued
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(("method", "url", "kwargs"), _READS)
async def test_a_read_past_the_wait_answers_503(env, short_wait, method, url, kwargs):
    url = url.format(run_id=env.run_id, sid=env.sid)
    resp = await _send_past_wait(env, method, url, **kwargs)
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == WRITER_BUSY_READ_DETAIL


# ---------------------------------------------------------------------------
# Writes: each case is (method, url, kwargs, check_done, check_unchanged).


async def _ns_description(env) -> str:
    meta = await env.storage.get_namespace_meta("default")
    return (meta or {}).get("description", "")


async def _scratch_value(env, key: str):
    entry = await env.storage.scratch_get(key)
    return entry["value"] if entry else None


async def _count(env) -> int:
    return len(await env.storage.list_chunks_by_source(env.note))


async def _feedback(env) -> list:
    return await env.storage.get_search_feedback(env.run_id)


async def _cases(env) -> list:
    return await env.storage.list_eval_cases()


def _write_cases():
    return {
        "namespace-meta": (
            "PATCH",
            "/api/namespaces/default",
            {"json": {"description": "edited"}},
            lambda env, before: _expect(_ns_description(env), "edited"),
            lambda env, before: _expect(_ns_description(env), before),
            _ns_description,
        ),
        "namespace-delete": (
            "DELETE",
            "/api/namespaces/default",
            {},
            lambda env, before: _expect(_count(env), 0),
            lambda env, before: _expect(_count(env), before),
            _count,
        ),
        "scratch-set": (
            "POST",
            "/api/scratch",
            {"json": {"key": "k2", "value": "v2"}},
            lambda env, before: _expect(_scratch_value(env, "k2"), "v2"),
            lambda env, before: _expect(_scratch_value(env, "k2"), None),
            lambda env: _scratch_value(env, "k2"),
        ),
        "scratch-delete": (
            "DELETE",
            "/api/scratch/k1",
            {},
            lambda env, before: _expect(_scratch_value(env, "k1"), None),
            lambda env, before: _expect(_scratch_value(env, "k1"), "v1"),
            lambda env: _scratch_value(env, "k1"),
        ),
        "source-delete": (
            "DELETE",
            "/api/sources",
            {"params": {"path": "{note}"}},
            lambda env, before: _expect(_count(env), 0),
            lambda env, before: _expect(_count(env), before),
            _count,
        ),
        "reset": (
            "POST",
            "/api/reset",
            {},
            lambda env, before: _expect(_count(env), 0),
            lambda env, before: _expect(_count(env), before),
            _count,
        ),
        "tag-rename": (
            "PUT",
            "/api/tags/t1",
            {"json": {"new_name": "t2"}},
            lambda env, before: _expect(_tags(env), [["t2"], ["t2"]]),
            lambda env, before: _expect(_tags(env), before),
            _tags,
        ),
        "tag-delete": (
            "DELETE",
            "/api/tags/t1",
            {},
            lambda env, before: _expect(_tags(env), [[], []]),
            lambda env, before: _expect(_tags(env), before),
            _tags,
        ),
        "tag-merge": (
            "POST",
            "/api/tags/merge",
            {"json": {"sources": ["t1"], "target": "t3"}},
            lambda env, before: _expect(_tags(env), [["t3"], ["t3"]]),
            lambda env, before: _expect(_tags(env), before),
            _tags,
        ),
        "feedback": (
            "POST",
            "/api/search/runs/{run_id}/feedback",
            {"json": {"chunk_id": "{snapshot_chunk_id}", "judgment": "relevant"}},
            lambda env, before: _expect_len(_feedback(env), 1),
            lambda env, before: _expect_len(_feedback(env), 0),
            _feedback,
        ),
    }


async def _expect(awaitable, expected) -> None:
    assert await awaitable == expected


async def _expect_len(awaitable, n: int) -> None:
    assert len(await awaitable) == n


def _fill(env, url: str, kwargs: dict) -> tuple[str, dict]:
    values = {
        "note": str(env.note),
        "chunk_id": env.chunk_id,
        "run_id": env.run_id,
        "snapshot_chunk_id": env.snapshot_chunk_id,
    }

    def sub(value):
        if isinstance(value, str):
            return value.format(**values)
        if isinstance(value, dict):
            return {k: sub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [sub(v) for v in value]
        return value

    return url.format(**values), sub(kwargs)


_WRITES = _write_cases()


@pytest.mark.parametrize("case", sorted(_WRITES))
async def test_a_write_waits_for_the_writer(env, case):
    method, url, kwargs, done, _unchanged, state = _WRITES[case]
    url, kwargs = _fill(env, url, kwargs)
    before = await state(env)
    queued, resp = await _send_while_owned(env, method, url, **kwargs)
    assert queued
    assert resp.status_code == 200, resp.text
    await done(env, before)


@pytest.mark.parametrize("case", sorted(_WRITES))
async def test_a_write_past_the_wait_answers_503_and_changes_nothing(env, short_wait, case):
    method, url, kwargs, _done, unchanged, state = _WRITES[case]
    url, kwargs = _fill(env, url, kwargs)
    before = await state(env)
    resp = await _send_past_wait(env, method, url, **kwargs)
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == WRITER_BUSY_NOTHING_CHANGED_DETAIL
    await unchanged(env, before)


async def test_the_rename_waits_and_a_timeout_is_the_busy_503(env, short_wait, monkeypatch):
    resp = await _send_past_wait(
        env, "POST", "/api/namespaces/default/rename", json={"new_name": "renamed"}
    )
    assert resp.status_code == 503, resp.text
    assert "nothing was changed" in resp.json()["detail"]
    assert await env.storage.count_chunks_by_namespace("renamed") == 0

    monkeypatch.setattr(backend_mod, "WRITER_WAIT_S", 5.0)
    queued, resp = await _send_while_owned(
        env, "POST", "/api/namespaces/default/rename", json={"new_name": "renamed"}
    )
    assert queued
    assert resp.status_code == 200, resp.text
    assert await env.storage.count_chunks_by_namespace("renamed") == 2


async def test_promotion_waits_for_the_writer_and_a_timeout_writes_nothing(
    env, short_wait, monkeypatch
):
    feedback = {"chunk_id": env.snapshot_chunk_id, "judgment": "relevant"}
    resp = await env.client.post(f"/api/search/runs/{env.run_id}/feedback", json=feedback)
    assert resp.status_code == 200, resp.text
    body = {"run_id": env.run_id, "name": "case-1"}

    resp = await _send_past_wait(env, "POST", "/api/quality/cases", json=body)
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == WRITER_BUSY_NOTHING_CHANGED_DETAIL
    assert await _cases(env) == []

    for mod in _WAIT_MODULES:
        monkeypatch.setattr(mod, "WRITER_WAIT_S", 5.0)
    queued, resp = await _send_while_owned(env, "POST", "/api/quality/cases", json=body)
    assert queued
    assert resp.status_code == 200, resp.text
    assert [c["name"] for c in await _cases(env)] == ["case-1"]


@pytest.mark.parametrize("url", ["/api/fts-rebuild", "/api/embedding-reset"])
async def test_an_idle_only_operation_waits_and_a_timeout_is_503(env, monkeypatch, url):
    queued, resp = await _send_while_owned(env, "POST", url)
    assert queued
    assert resp.status_code == 200, resp.text

    for mod in _WAIT_MODULES:
        monkeypatch.setattr(mod, "WRITER_WAIT_S", 0.05)
    resp = await _send_past_wait(env, "POST", url)
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == WRITER_BUSY_NOTHING_CHANGED_DETAIL


async def test_scratch_promote_waits_for_the_writer(env, short_wait, monkeypatch):
    before = sorted(p.name for p in env.mem_dir.iterdir())
    resp = await _send_past_wait(env, "POST", "/api/scratch/k1/promote", json={})
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == WRITER_BUSY_NOTHING_CHANGED_DETAIL
    assert sorted(p.name for p in env.mem_dir.iterdir()) == before
    assert (await env.storage.scratch_get("k1"))["promoted"] is False

    for mod in _WAIT_MODULES:
        monkeypatch.setattr(mod, "WRITER_WAIT_S", 5.0)
    queued, resp = await _send_while_owned(env, "POST", "/api/scratch/k1/promote", json={})
    assert queued
    assert resp.status_code == 200, resp.text
    assert (await env.storage.scratch_get("k1"))["promoted"] is True


# ---------------------------------------------------------------------------
# Ordering and consent


async def test_source_delete_rejudges_consent_after_its_wait(env):
    """A source made project_shared while the delete waits is not deleted unconfirmed."""

    async def make_shared(storage) -> None:
        storage._get_db().execute(
            "UPDATE chunks SET scope = 'project_shared' WHERE source_file = ?",
            (str(env.note),),
        )

    before = await _count(env)
    queued, resp = await _send_while_owned(
        env, "DELETE", "/api/sources", params={"path": str(env.note)}, inside=make_shared
    )
    assert queued
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "needs_confirmation", resp.json()
    assert await _count(env) == before


async def test_a_waiting_tag_write_does_not_hold_the_tag_lock(env):
    """The index path owns the writer and then takes the tag lock.

    A tag write that held the tag lock while it waited for the writer would
    stall that index until its own wait ran out.
    """
    took_tag_lock: list[bool] = []

    async def take_tag_lock(storage) -> None:
        async with asyncio.timeout(1):
            async with storage._tag_write_lock:
                took_tag_lock.append(True)

    queued, resp = await _send_while_owned(
        env, "PUT", "/api/tags/t1", json={"new_name": "t2"}, inside=take_tag_lock
    )
    assert queued
    assert took_tag_lock == [True]
    assert resp.status_code == 200, resp.text


async def test_idle_only_operations_still_refuse_inside_their_callers_transaction(env):
    storage = env.storage
    with pytest.raises(StorageError, match="cannot run while a transaction is active"):
        async with storage.transaction():
            await storage.rebuild_fts()
    info = storage.stored_embedding_info
    with pytest.raises(StorageError, match="cannot run while a transaction is active"):
        async with storage.transaction():
            await storage.reset_embedding_meta(dimension=info["dimension"])


async def test_the_rename_waits_for_a_writer_taken_while_it_locked_its_sources(env, monkeypatch):
    """The owner takes the writer while the rename acquires its file locks.

    The planning read has already passed, so the reload under the locks must
    wait for it; nothing suspends between that reload and the write.
    """
    owner = _Owner(env.storage)
    real_lock = ns_mgmt_mod.async_file_lock

    def lock_then_own(lock_path, *, timeout):
        @asynccontextmanager
        async def wrapped():
            async with real_lock(lock_path, timeout=timeout):
                if owner.task is None:
                    await owner.start()
                yield

        return wrapped()

    monkeypatch.setattr(ns_mgmt_mod, "async_file_lock", lock_then_own)
    task = asyncio.ensure_future(
        env.client.post("/api/namespaces/default/rename", json={"new_name": "renamed"})
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not _queued(env.storage) and not task.done() and loop.time() < deadline:
        await asyncio.sleep(0.005)
    queued = _queued(env.storage) and not task.done()
    await owner.finish()
    resp = await asyncio.wait_for(task, 10)
    assert queued
    assert resp.status_code == 200, resp.text
    assert await env.storage.count_chunks_by_namespace("renamed") == 2


async def test_replay_waits_for_a_writer_taken_during_an_earlier_case(env, monkeypatch):
    """The owner takes the writer during the first case's search.

    The second case's ``get_eval_case`` then reaches the writer after that
    ``await``.
    """
    feedback = {"chunk_id": env.snapshot_chunk_id, "judgment": "relevant"}
    resp = await env.client.post(f"/api/search/runs/{env.run_id}/feedback", json=feedback)
    assert resp.status_code == 200, resp.text
    for name in ("case-1", "case-2"):
        resp = await env.client.post(
            "/api/quality/cases", json={"run_id": env.run_id, "name": name}
        )
        assert resp.status_code == 200, resp.text

    owner = _Owner(env.storage)
    pipeline = env.comp.search_pipeline
    real_search = pipeline.search

    async def search_then_own(*args, **kwargs):
        result = await real_search(*args, **kwargs)
        if owner.task is None:
            await owner.start()
        return result

    monkeypatch.setattr(pipeline, "search", search_then_own)
    task = asyncio.ensure_future(env.client.post("/api/quality/replay", json={}))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not _queued(env.storage) and not task.done() and loop.time() < deadline:
        await asyncio.sleep(0.005)
    queued = _queued(env.storage) and not task.done()
    await owner.finish()
    resp = await asyncio.wait_for(task, 10)
    assert queued
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["cases"]) == 2


async def test_promote_search_run_waits_for_the_writer(env):
    """The storage method itself waits, for callers without the route's fingerprint wait."""
    feedback = {"chunk_id": env.snapshot_chunk_id, "judgment": "relevant"}
    resp = await env.client.post(f"/api/search/runs/{env.run_id}/feedback", json=feedback)
    assert resp.status_code == 200, resp.text
    fingerprints, _ = quality_state_mod.current_fingerprints(env.storage, env.comp.config)

    owner = _Owner(env.storage)
    await owner.start()
    task = asyncio.ensure_future(
        env.storage.promote_search_run(env.run_id, name="case-1", fingerprints=fingerprints)
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not _queued(env.storage) and not task.done() and loop.time() < deadline:
        await asyncio.sleep(0.005)
    queued = _queued(env.storage) and not task.done()
    await owner.finish()
    case = await asyncio.wait_for(task, 10)
    assert queued
    assert case["name"] == "case-1"


async def test_a_per_chunk_tag_edit_refuses_at_once_with_503(env):
    """The per-chunk tag edit does not wait (see ``tag_management``'s docstring).

    Its refusal comes before its write, so it answers 503 "nothing was
    changed" instead of the generic 500, without queueing.
    """
    before = await _tags(env)
    owner = _Owner(env.storage)
    await owner.start()
    try:
        resp = await asyncio.wait_for(
            env.client.patch(f"/api/chunks/{env.chunk_id}/tags", json={"tags": ["t9"]}), 10
        )
        assert not _queued(env.storage)
    finally:
        await owner.finish()
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == WRITER_BUSY_REFUSED_NOTHING_CHANGED_DETAIL
    assert await _tags(env) == before


async def test_get_search_feedback_waits_for_the_writer(env):
    """The run-detail route reaches it right after ``get_search_run``'s wait, so
    only a direct call shows its own wait."""
    owner = _Owner(env.storage)
    await owner.start()
    task = asyncio.ensure_future(env.storage.get_search_feedback(env.run_id))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not _queued(env.storage) and not task.done() and loop.time() < deadline:
        await asyncio.sleep(0.005)
    queued = _queued(env.storage) and not task.done()
    await owner.finish()
    assert await asyncio.wait_for(task, 10) == []
    assert queued


async def test_a_rename_whose_locked_reload_times_out_is_the_busy_503(env, short_wait, monkeypatch):
    """The owner takes the writer while the rename locks its sources and keeps it."""
    owner = _Owner(env.storage)
    real_lock = ns_mgmt_mod.async_file_lock

    def lock_then_own(lock_path, *, timeout):
        @asynccontextmanager
        async def wrapped():
            async with real_lock(lock_path, timeout=timeout):
                if owner.task is None:
                    await owner.start()
                yield

        return wrapped()

    monkeypatch.setattr(ns_mgmt_mod, "async_file_lock", lock_then_own)
    try:
        resp = await asyncio.wait_for(
            env.client.post("/api/namespaces/default/rename", json={"new_name": "renamed"}), 10
        )
    finally:
        await owner.finish()
    assert resp.status_code == 503, resp.text
    assert "nothing was changed" in resp.json()["detail"]
    assert await env.storage.count_chunks_by_namespace("renamed") == 0
