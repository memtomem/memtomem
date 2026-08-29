"""The skills routes stop their workers once the request is gone (#2247).

The engine-level checks live in ``test_skills_sync_abandon.py``; these cover
the wiring — that the routes actually enter ``abandon_sync_on_exit`` around
their ``asyncio.to_thread`` hand-off, which is what makes those checks
reachable — and the CRUD closures, whose checks have no engine test because
they are route-local.

The end-to-end case parks the worker inside the engine, cancels the request
task the way the route's ``asyncio.timeout`` would, then releases the worker
and asserts nothing was written. Modelled on
``test_settings_home_pinning.py::test_a_cancelled_sync_does_not_write``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from memtomem.context import skills as skills_mod
from memtomem.web.routes import context_skills

pytestmark = pytest.mark.anyio

SKILL_MD = """---
name: alpha
description: A skill.
---

Body.
"""


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    skill = root / ".memtomem" / "skills" / "alpha"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    return root


def _observe_worker(monkeypatch) -> threading.Event:
    """Signal when the dispatched CRUD closure itself returns.

    Joining on the cancelled *task* is not the same thing and is the trap this
    exists to avoid: the task's ``finally`` runs the moment it is cancelled,
    while the worker thread it can no longer wait for is still running. A test
    that reads the filesystem at that point wins a race it did not intend to
    run, and passes with the abort check deleted.

    Wraps ``asyncio.to_thread`` for closures only, so the test's own
    ``to_thread(event.wait, ...)`` calls are untouched.
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


def _abandon_flag() -> threading.Event:
    """The live abort ``Event`` the route's scope installed for this dispatch."""
    from memtomem.context import _abandon

    event = _abandon._sync_abandoned.get()
    assert event is not None, "the route did not enter abandon_sync_on_exit"
    return event


def _abandon_before_the_worker_runs(monkeypatch) -> threading.Event:
    """Reproduce the queued-worker window: cancelled before the closure starts.

    ``asyncio.to_thread`` hands the closure to an executor; nothing guarantees
    the thread picks it up before the caller is cancelled. Only the pre-lock
    checkpoint can fire in that window, and only it leaves *no* trace — every
    later check has already acquired the lock and created a sidecar nothing
    removes. Parking inside the lock (the other harness here) cannot reach this
    window, which is how the pre-lock checks first looked redundant: the probe
    could not get there, not the code.
    """
    done = threading.Event()
    real_to_thread = asyncio.to_thread

    async def _patched(fn, /, *args, **kwargs):
        if getattr(fn, "__name__", "").endswith("_locked"):

            def _wrapped(*a, **k):
                _abandon_flag().set()
                try:
                    return fn(*a, **k)
                finally:
                    done.set()

            return await real_to_thread(_wrapped, *args, **kwargs)
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _patched)
    return done


def _fanned_out(project_root: Path) -> list[Path]:
    return [
        dst
        for gen in skills_mod.SKILL_GENERATORS.values()
        if (dst := gen.target_dir(project_root, "alpha", scope="project_shared")) is not None
        and dst.exists()
    ]


class TestSyncRoute:
    async def test_a_cancelled_sync_fans_nothing_out(self, tmp_path, monkeypatch):
        project_root = _project(tmp_path)
        assert _fanned_out(project_root) == [], "fixture should start unsynced"

        # Park the worker inside the engine, after dispatch and before any
        # promote, so the cancellation lands where a real timeout would.
        entered, release = threading.Event(), threading.Event()
        worker_done = threading.Event()
        real_engine = context_skills.generate_all_skills

        def _blocking_engine(*args, **kwargs):
            entered.set()
            release.wait(timeout=10)
            try:
                return real_engine(*args, **kwargs)
            finally:
                worker_done.set()

        monkeypatch.setattr(context_skills, "generate_all_skills", _blocking_engine)

        task = asyncio.create_task(context_skills._sync_skills_core(project_root, "project_shared"))
        try:
            await asyncio.to_thread(entered.wait, 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            assert await asyncio.to_thread(worker_done.wait, 20), "orphaned worker never finished"
        finally:
            release.set()

        assert _fanned_out(project_root) == [], (
            "the orphaned worker fanned skills out after the request was "
            "cancelled — the user was told the sync failed and the runtime "
            "directories changed regardless"
        )


class TestCrudClosures:
    """One check each, all placed so an abandoned request leaves no change."""

    async def test_a_cancelled_create_writes_no_skill(self, tmp_path, monkeypatch):
        project_root = tmp_path / "project"
        (project_root / ".memtomem" / "skills").mkdir(parents=True)

        worker_done = _observe_worker(monkeypatch)
        entered, release = threading.Event(), threading.Event()
        real_lock = context_skills.canonical_sidecar_lock

        def _blocking_lock(*args, **kwargs):
            # Block on the way IN to the lock — the wait this fix is about.
            entered.set()
            release.wait(timeout=10)
            return real_lock(*args, **kwargs)

        monkeypatch.setattr(context_skills, "canonical_sidecar_lock", _blocking_lock)

        body = context_skills.SkillCreateRequest(name="alpha", content=SKILL_MD)

        task = asyncio.create_task(
            context_skills.create_skill(
                body, project_root=project_root, target_scope="project_shared"
            )
        )
        try:
            await asyncio.to_thread(entered.wait, 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            assert await asyncio.to_thread(worker_done.wait, 20), (
                "the dispatched closure never returned — the assertion below "
                "would be reading the filesystem before the worker touched it"
            )
        finally:
            release.set()

        assert not (project_root / ".memtomem" / "skills" / "alpha").exists(), (
            "an abandoned create landed a skill the caller was told failed"
        )

    async def test_a_cancelled_update_leaves_the_manifest_alone(self, tmp_path, monkeypatch):
        project_root = _project(tmp_path)
        manifest = project_root / ".memtomem" / "skills" / "alpha" / "SKILL.md"
        before = manifest.read_text(encoding="utf-8")
        mtime_ns = manifest.stat().st_mtime_ns

        worker_done = _observe_worker(monkeypatch)
        entered, release = threading.Event(), threading.Event()
        real_lock = context_skills.canonical_sidecar_lock

        def _blocking_lock(*args, **kwargs):
            entered.set()
            release.wait(timeout=10)
            return real_lock(*args, **kwargs)

        monkeypatch.setattr(context_skills, "canonical_sidecar_lock", _blocking_lock)

        body = context_skills.SkillUpdateRequest(
            content="---\nname: alpha\ndescription: Edited.\n---\n\nEdited.\n",
            mtime_ns=str(mtime_ns),
        )

        task = asyncio.create_task(
            context_skills.update_skill(
                "alpha", body, project_root=project_root, target_scope="project_shared"
            )
        )
        try:
            await asyncio.to_thread(entered.wait, 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            assert await asyncio.to_thread(worker_done.wait, 20), (
                "the dispatched closure never returned — the assertion below "
                "would be reading the filesystem before the worker touched it"
            )
        finally:
            release.set()

        assert manifest.read_text(encoding="utf-8") == before, (
            "an abandoned update rewrote the manifest behind a failed request"
        )

    async def test_a_cancelled_delete_removes_nothing(self, tmp_path, monkeypatch):
        """The delete's checkpoint sits before its first removal.

        The canonical removal and the runtime cascade are one sequence with no
        lock spanning both, so stopping partway would orphan the runtime
        copies. Either it has not started, or it finishes — which is why the
        one check sits after the lock is held (the wait the request timed out
        on) and before anything is removed, never inside the sequence.
        """
        project_root = _project(tmp_path)
        skill_dir = project_root / ".memtomem" / "skills" / "alpha"

        worker_done = _observe_worker(monkeypatch)
        entered, release = threading.Event(), threading.Event()
        real_lock = context_skills.canonical_sidecar_lock

        def _blocking_lock(*args, **kwargs):
            entered.set()
            release.wait(timeout=10)
            return real_lock(*args, **kwargs)

        monkeypatch.setattr(context_skills, "canonical_sidecar_lock", _blocking_lock)

        task = asyncio.create_task(
            context_skills.delete_skill(
                "alpha", project_root=project_root, target_scope="project_shared"
            )
        )
        try:
            await asyncio.to_thread(entered.wait, 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            assert await asyncio.to_thread(worker_done.wait, 20), (
                "the dispatched closure never returned — the assertion below "
                "would be reading the filesystem before the worker touched it"
            )
        finally:
            release.set()

        assert skill_dir.exists(), (
            "an abandoned delete removed the canonical skill behind a failed request"
        )
        assert json.loads(json.dumps(sorted(p.name for p in skill_dir.iterdir()))) == ["SKILL.md"]


@pytest.mark.parametrize("op", ["create", "update", "delete"])
async def test_a_worker_abandoned_while_queued_leaves_no_trace(tmp_path, monkeypatch, op):
    """The pre-lock checkpoint, which no post-lock check can substitute for.

    A closure that never started before its caller was cancelled must not
    acquire the lock at all — acquisition leaves a sidecar nothing ever
    removes, and on the skills delete path it also runs swap recovery, so
    "the request failed and changed nothing" would be false in the one place a
    user could still see it.
    """
    project_root = _project(tmp_path)
    skills_dir = project_root / ".memtomem" / "skills"
    manifest = skills_dir / "alpha" / "SKILL.md"
    worker_done = _abandon_before_the_worker_runs(monkeypatch)

    if op == "create":
        coro = context_skills.create_skill(
            context_skills.SkillCreateRequest(name="beta", content=SKILL_MD),
            project_root=project_root,
            target_scope="project_shared",
        )
    elif op == "update":
        coro = context_skills.update_skill(
            "alpha",
            context_skills.SkillUpdateRequest(
                content=SKILL_MD, mtime_ns=str(manifest.stat().st_mtime_ns)
            ),
            project_root=project_root,
            target_scope="project_shared",
        )
    else:
        coro = context_skills.delete_skill(
            "alpha", project_root=project_root, target_scope="project_shared"
        )

    try:
        await coro
    except Exception:
        # The route may still raise its own envelope off the no-op arm; what is
        # under test is the filesystem, not the response.
        pass
    assert await asyncio.to_thread(worker_done.wait, 20), "the closure never ran"

    sidecars = sorted(p.name for p in skills_dir.iterdir() if p.name.endswith(".lock"))
    assert sidecars == [], (
        f"a worker abandoned while queued still acquired its lock: {sidecars} — "
        "the pre-lock checkpoint did not fire"
    )
