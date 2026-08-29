"""The inline-rule routes stop writing once their caller has given up (#2247).

``resolve`` / ``delete`` / ``promote`` all commit through one helper,
``_locked_cas_write``, offloaded to ``asyncio.to_thread`` under a 60s route
timeout. The offload keeps the portalocker wait off the event loop, but a
thread cannot be cancelled: when the timeout fires the route answers 503 and
the worker goes on to write the user's settings file. One check inside the
helper covers all three routes — it is a single write with no legs to leave
half-done, so suppressing it is unambiguous.

Two levels here: the helper on its own (fast, exact about placement) and one
end-to-end cancellation of the resolve route (the shape the fix exists for,
modelled on ``test_settings_home_pinning.py``).
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from memtomem.context._abandon import abandon_sync_on_exit
from memtomem.web.routes import settings_sync

pytestmark = pytest.mark.anyio

CANONICAL_RULE = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _project(tmp_path: Path) -> Path:
    """A project whose target tier carries a rule that conflicts with canonical."""
    root = tmp_path / "project"
    _write(root / ".memtomem" / "settings.json", {"hooks": {"PreToolUse": [CANONICAL_RULE]}})
    _write(
        root / ".claude" / "settings.json",
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo drifted"}]}
                ]
            }
        },
    )
    return root


def _target(root: Path) -> Path:
    return root / ".claude" / "settings.json"


class TestTheHelper:
    """``_locked_cas_write`` refuses the write when the flag is set."""

    def test_an_abandoned_write_does_not_land(self, tmp_path):
        root = _project(tmp_path)
        target = _target(root)
        before = target.read_text(encoding="utf-8")
        mtime_ns = target.stat().st_mtime_ns

        with abandon_sync_on_exit() as abandoned:
            abandoned.set()
            wrote, current = settings_sync._locked_cas_write(target, mtime_ns, {"hooks": {}})

        assert wrote is False
        assert current == mtime_ns, "reported an mtime the caller did not cause"
        assert target.read_text(encoding="utf-8") == before, (
            "the helper wrote behind a caller that had already returned 503"
        )

    def test_a_write_nobody_abandoned_lands(self, tmp_path):
        """The default: nothing entered the scope, so nothing changes."""
        root = _project(tmp_path)
        target = _target(root)
        mtime_ns = target.stat().st_mtime_ns

        wrote, _current = settings_sync._locked_cas_write(target, mtime_ns, {"hooks": {"X": []}})

        assert wrote is True
        assert json.loads(target.read_text(encoding="utf-8")) == {"hooks": {"X": []}}

    def test_a_stale_mtime_still_reports_the_conflict(self, tmp_path):
        """The abort must not swallow the pre-existing 409 path."""
        root = _project(tmp_path)
        target = _target(root)

        wrote, current = settings_sync._locked_cas_write(target, 1, {"hooks": {}})

        assert wrote is False
        assert current == target.stat().st_mtime_ns


class TestTheRoute:
    """End-to-end: cancel the request, release the worker, expect no write."""

    async def test_a_cancelled_resolve_does_not_write(self, tmp_path, monkeypatch):
        root = _project(tmp_path)
        target = _target(root)
        before = target.read_text(encoding="utf-8")

        # Park the worker inside the helper, after the dispatch and before any
        # write, so the cancellation lands exactly where a real timeout would.
        entered, release = threading.Event(), threading.Event()
        worker_done = threading.Event()
        real_helper = settings_sync._locked_cas_write

        def _blocking_helper(path, expected_mtime_ns, doc):
            entered.set()
            release.wait(timeout=10)
            try:
                return real_helper(path, expected_mtime_ns, doc)
            finally:
                worker_done.set()

        monkeypatch.setattr(settings_sync, "_locked_cas_write", _blocking_helper)

        task = asyncio.create_task(
            settings_sync.resolve_conflict(
                settings_sync.ResolveRequest(event="PreToolUse", matcher="Bash"),
                project_root=root,
                target_scope="project_shared",
            )
        )
        try:
            await asyncio.to_thread(entered.wait, 10)
            # What the route's ``asyncio.timeout`` does when it fires.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            assert await asyncio.to_thread(worker_done.wait, 20), "orphaned worker never finished"
        finally:
            release.set()

        assert target.read_text(encoding="utf-8") == before, (
            "the orphaned worker resolved the conflict after the request was "
            "cancelled — the user was told the resolve failed and the file "
            "changed regardless"
        )
