"""The shared agents/commands CRUD routes honour the abort flag too (#2247).

``web/routes/_atomic_kind.py`` is the collapsed implementation behind every
agent and command create / update / delete, and its three locked closures have
the same names as the skills ones. That name sharing is why the dispatch guard
lists each closure with a floor of two, and it is why these tests exist: the
skills coverage in ``test_skills_routes_abandon.py`` would stay green with
every checkpoint in this module deleted.

Agents stand in for both kinds — one ``AtomicKindSpec`` drives both, so the
checkpoint placement under test is literally the same code.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from memtomem.web.routes import context_agents

pytestmark = pytest.mark.anyio

AGENT_MD = """---
name: alpha
description: An agent.
---

Body.
"""


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".memtomem" / "agents").mkdir(parents=True)
    return root


def _seeded(tmp_path: Path) -> tuple[Path, Path]:
    root = _project(tmp_path)
    path = root / ".memtomem" / "agents" / "alpha.md"
    path.write_text(AGENT_MD, encoding="utf-8")
    return root, path


def _observe_worker(monkeypatch) -> threading.Event:
    """Signal when the dispatched closure itself returns — not when the task dies.

    The cancelled task's ``finally`` runs immediately; the worker it can no
    longer wait for is still going. Asserting at that moment reads the
    filesystem before the write it is looking for, and passes with the
    checkpoint deleted.
    """
    done = threading.Event()
    real_to_thread = asyncio.to_thread

    async def _patched(fn, /, *args, **kwargs):
        if getattr(fn, "__name__", "").endswith("_locked"):

            def _wrapped(*a, **k):
                try:
                    return fn(*a, **k)
                finally:
                    done.set()

            return await real_to_thread(_wrapped, *args, **kwargs)
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _patched)
    return done


def _abandon_before_the_worker_runs(monkeypatch) -> threading.Event:
    """Reproduce the queued-worker window: cancelled before the closure starts.

    ``asyncio.to_thread`` hands the closure to an executor; there is no
    guarantee the thread picks it up before the caller is cancelled. In that
    window the pre-lock checkpoint is the only one that can fire, and it is the
    only one that leaves *no* trace — every later check has already acquired
    the lock and created a sidecar nothing removes.

    Blocking inside the lock (the other harness here) cannot reach this window,
    which is how the pre-lock checks first looked redundant: the probe could
    not get there, not the code.
    """
    done = threading.Event()
    real_to_thread = asyncio.to_thread

    async def _patched(fn, /, *args, **kwargs):
        if getattr(fn, "__name__", "").endswith("_locked"):

            def _wrapped(*a, **k):
                # What a cancelled caller's ``abandon_sync_on_exit`` finally
                # does, timed as if it had happened while this sat in the queue.
                _abandon_flag_from(fn).set()
                try:
                    return fn(*a, **k)
                finally:
                    done.set()

            return await real_to_thread(_wrapped, *args, **kwargs)
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _patched)
    return done


def _abandon_flag_from(fn) -> threading.Event:
    """The live abort ``Event`` the route's scope installed for this dispatch."""
    from memtomem.context import _abandon

    event = _abandon._sync_abandoned.get()
    assert event is not None, "the route did not enter abandon_sync_on_exit"
    return event


def _block_the_lock(monkeypatch) -> tuple[threading.Event, threading.Event]:
    """Park the worker on the way IN to the canonical lock — the wait at issue."""
    entered, release = threading.Event(), threading.Event()
    real_lock = context_agents._atomic_kind.canonical_sidecar_lock

    def _blocking_lock(*args, **kwargs):
        entered.set()
        release.wait(timeout=10)
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(context_agents._atomic_kind, "canonical_sidecar_lock", _blocking_lock)
    return entered, release


async def _cancel_after_entering(task, entered, release, worker_done):
    await asyncio.to_thread(entered.wait, 10)
    # What the route's ``asyncio.timeout`` does when it fires.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.to_thread(worker_done.wait, 20), (
        "the dispatched closure never returned — the assertion would be "
        "reading the filesystem before the worker touched it"
    )


async def test_a_cancelled_create_writes_no_agent(tmp_path, monkeypatch):
    project_root = _project(tmp_path)
    worker_done = _observe_worker(monkeypatch)
    entered, release = _block_the_lock(monkeypatch)

    body = context_agents.AgentCreateRequest(name="alpha", content=AGENT_MD)
    task = asyncio.create_task(
        context_agents.create_agent(body, project_root=project_root, target_scope="project_shared")
    )
    try:
        await _cancel_after_entering(task, entered, release, worker_done)
    finally:
        release.set()

    # The lock sidecar is not counted: acquiring the lock is what creates it,
    # and the checkpoint has to sit *after* the acquisition to cover the wait
    # the request timed out on. What must not exist is the artifact.
    landed = sorted(
        p.name
        for p in (project_root / ".memtomem" / "agents").iterdir()
        if not p.name.endswith(".lock")
    )
    assert landed == [], f"an abandoned create landed {landed} behind a failed request"


async def test_a_cancelled_update_leaves_the_file_alone(tmp_path, monkeypatch):
    project_root, path = _seeded(tmp_path)
    before = path.read_text(encoding="utf-8")

    worker_done = _observe_worker(monkeypatch)
    entered, release = _block_the_lock(monkeypatch)

    body = context_agents.AgentUpdateRequest(
        content="---\nname: alpha\ndescription: Edited.\n---\n\nEdited.\n",
        mtime_ns=str(path.stat().st_mtime_ns),
    )
    task = asyncio.create_task(
        context_agents.update_agent(
            "alpha", body, project_root=project_root, target_scope="project_shared"
        )
    )
    try:
        await _cancel_after_entering(task, entered, release, worker_done)
    finally:
        release.set()

    assert path.read_text(encoding="utf-8") == before, (
        "an abandoned update rewrote the agent behind a failed request"
    )


async def test_a_cancelled_delete_removes_nothing(tmp_path, monkeypatch):
    """The checkpoint sits after the lock is held and before any removal.

    After, because the lock wait is what the request timed out on; before,
    because the canonical removal and the runtime cascade are one sequence
    that must not be stopped partway.
    """
    project_root, path = _seeded(tmp_path)

    worker_done = _observe_worker(monkeypatch)
    entered, release = _block_the_lock(monkeypatch)

    task = asyncio.create_task(
        context_agents.delete_agent(
            "alpha", cascade=False, project_root=project_root, target_scope="project_shared"
        )
    )
    try:
        await _cancel_after_entering(task, entered, release, worker_done)
    finally:
        release.set()

    assert path.is_file(), "an abandoned delete removed the agent behind a failed request"


@pytest.mark.parametrize("op", ["create", "update", "delete"])
async def test_a_worker_abandoned_while_queued_leaves_no_trace(tmp_path, monkeypatch, op):
    """The pre-lock checkpoint, which no post-lock check can substitute for.

    A closure that never started before its caller was cancelled must not
    acquire the lock at all — acquisition leaves a sidecar that nothing ever
    removes, so "the request failed and changed nothing" would be false in the
    one place a user could still see it.
    """
    project_root, path = _seeded(tmp_path)
    agents_dir = project_root / ".memtomem" / "agents"
    worker_done = _abandon_before_the_worker_runs(monkeypatch)

    if op == "create":
        (agents_dir / "alpha.md").unlink()
        coro = context_agents.create_agent(
            context_agents.AgentCreateRequest(name="alpha", content=AGENT_MD),
            project_root=project_root,
            target_scope="project_shared",
        )
    elif op == "update":
        coro = context_agents.update_agent(
            "alpha",
            context_agents.AgentUpdateRequest(
                content=AGENT_MD, mtime_ns=str(path.stat().st_mtime_ns)
            ),
            project_root=project_root,
            target_scope="project_shared",
        )
    else:
        coro = context_agents.delete_agent(
            "alpha", cascade=False, project_root=project_root, target_scope="project_shared"
        )

    try:
        await coro
    except Exception:
        # The route may still raise its own 404/409 envelope off the no-op
        # arm; what is under test is the filesystem, not the response.
        pass
    assert await asyncio.to_thread(worker_done.wait, 20), "the closure never ran"

    sidecars = sorted(p.name for p in agents_dir.iterdir() if p.name.endswith(".lock"))
    assert sidecars == [], (
        f"a worker abandoned while queued still acquired its lock: {sidecars} — "
        "the pre-lock checkpoint did not fire"
    )
