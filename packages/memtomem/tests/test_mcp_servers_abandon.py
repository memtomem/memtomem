"""``generate_all_mcp_servers`` stops writing once its caller is gone (#2247).

The simplest engine in the series: one target, one write, so unlike the skills
and settings engines there is no partial state to reason about — the sync
either writes ``.mcp.json`` or it does not. The three checkpoints exist to
cover the three stretches a caller can give up in: before the canonical scan,
while waiting for the sidecar lock, and during the read/merge/serialize that
follows it.

This engine reached the abort late for a reason worth recording: its route
called it *synchronously*, so there was no orphaned worker to stop — the
blocking lock wait simply froze the event loop instead. The route now offloads
to a worker thread, which fixes that and creates the orphan window in the same
change, which is why the checks land with it.

Everything writes into ``tmp_path``; ``.mcp.json`` is project-rooted, so no
host home is involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memtomem.context import _skip_reasons as skip_codes
from memtomem.context import mcp_servers as mcp_mod
from memtomem.context._abandon import abandon_sync_on_exit, sync_is_abandoned
from memtomem.context.mcp_servers import PROJECT_MCP_CONFIG, generate_all_mcp_servers

DEFINITION = {"command": "uvx", "args": ["some-server"]}


def _canonical(root: Path, name: str = "demo") -> Path:
    path = root / ".memtomem" / "mcp-servers" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFINITION, indent=2) + "\n", encoding="utf-8")
    return path


def _target(root: Path) -> Path:
    return root / PROJECT_MCP_CONFIG


def _abandoned_rows(result):
    return [row for row in result.skipped if row[2] == skip_codes.ABANDONED]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _canonical(root)
    return root


def test_a_sync_abandoned_before_it_starts_writes_nothing(project):
    with abandon_sync_on_exit() as abandoned:
        abandoned.set()
        result = generate_all_mcp_servers(project)

    assert result.generated == []
    assert [row[2] for row in result.skipped] == [skip_codes.ABANDONED]
    assert "abandoned by its caller" in result.skipped[0][1]
    assert not _target(project).exists()
    # The entry check runs before the scan and before any lock, so an
    # abandoned sync leaves no sidecar either — acquiring one creates a file
    # nothing removes.
    assert sorted(project.rglob(".*.lock")) == []


def test_abandonment_while_waiting_for_the_lock_stops_before_the_read(project, monkeypatch):
    """The post-lock check, distinguished from the pre-write one.

    The pre-write check would also leave ``.mcp.json`` alone, so the outcome
    cannot tell them apart. Only the post-lock check makes it true that the
    target was never even read.
    """
    # The target has to EXIST for this to prove anything: the parse it asserts
    # against only runs when there is a file to parse, so against a fresh
    # project the assertion would hold with the check deleted.
    target = _target(project)
    target.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    before = target.read_text(encoding="utf-8")

    real_lock = mcp_mod._file_lock
    reads: list = []

    with abandon_sync_on_exit() as abandoned:

        def _lock_then_abandon(path, timeout):
            ctx = real_lock(path, timeout=timeout)
            abandoned.set()
            return ctx

        def _record_parse(*args, **kwargs):
            reads.append(args)
            raise AssertionError("the target must not be parsed for an abandoned sync")

        monkeypatch.setattr(mcp_mod, "_file_lock", _lock_then_abandon)
        monkeypatch.setattr(mcp_mod, "_parse_project_mcp_text", _record_parse)
        result = generate_all_mcp_servers(project)

    assert reads == [], "the sync parsed the target after its caller gave up"
    assert target.read_text(encoding="utf-8") == before
    assert _abandoned_rows(result), result.skipped


def test_abandonment_during_the_merge_suppresses_the_write(project, monkeypatch):
    """The pre-write check: the merge and serialize run after the post-lock one."""
    real_parse = mcp_mod._parse_project_mcp_text

    with abandon_sync_on_exit() as abandoned:

        def _parse_then_abandon(*args, **kwargs):
            out = real_parse(*args, **kwargs)
            abandoned.set()
            return out

        monkeypatch.setattr(mcp_mod, "_parse_project_mcp_text", _parse_then_abandon)
        _target(project).write_text('{"mcpServers": {}}\n', encoding="utf-8")
        before = _target(project).read_text(encoding="utf-8")
        result = generate_all_mcp_servers(project)

    assert _target(project).read_text(encoding="utf-8") == before, (
        "the merge completed and the write landed behind a caller that had given up"
    )
    assert _abandoned_rows(result), result.skipped


def test_foreign_entries_survive_an_abandoned_sync(project):
    """The file a user could actually lose: an abandoned sync must not rewrite it."""
    target = _target(project)
    target.write_text(
        json.dumps({"mcpServers": {"someone-elses": {"command": "keep-me"}}}, indent=2) + "\n",
        encoding="utf-8",
    )
    before = target.read_text(encoding="utf-8")

    with abandon_sync_on_exit() as abandoned:
        abandoned.set()
        generate_all_mcp_servers(project)

    assert target.read_text(encoding="utf-8") == before


def test_a_sync_nobody_abandoned_writes_normally(project):
    """The default: the CLI never enters the scope, so nothing changes for it."""
    result = generate_all_mcp_servers(project)

    assert result.generated, result
    assert not _abandoned_rows(result)
    assert "demo" in json.loads(_target(project).read_text(encoding="utf-8"))["mcpServers"]
    assert sync_is_abandoned() is False


class TestTheRoute:
    """End-to-end: the offload is what makes the checks reachable at all."""

    pytestmark = pytest.mark.anyio

    async def test_a_cancelled_sync_does_not_write(self, project, monkeypatch):
        import asyncio
        import threading

        from memtomem.web.routes import context_mcp_servers

        target = _target(project)
        assert not target.exists(), "fixture should start unsynced"

        # Park the worker inside the engine, after dispatch and before the
        # write, so the cancellation lands where a real timeout would.
        entered, release = threading.Event(), threading.Event()
        worker_done = threading.Event()
        real_engine = context_mcp_servers.generate_all_mcp_servers

        def _blocking_engine(*args, **kwargs):
            entered.set()
            release.wait(timeout=10)
            try:
                return real_engine(*args, **kwargs)
            finally:
                worker_done.set()

        monkeypatch.setattr(context_mcp_servers, "generate_all_mcp_servers", _blocking_engine)

        task = asyncio.create_task(context_mcp_servers._sync_mcp_servers_core(project))
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

        assert not target.exists(), (
            "the orphaned worker wrote .mcp.json after the request was cancelled — "
            "the user was told the sync failed and the file changed regardless"
        )
