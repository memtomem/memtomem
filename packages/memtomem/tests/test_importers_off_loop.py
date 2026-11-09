"""Notion and Obsidian imports run on a worker thread, not the event loop (#3124).

The importer bodies read archives, walk directories and fsync files. On the loop
they stalled every other request the MCP server was serving. These tests pin
that the I/O happens off the loop thread, that the loop keeps running during an
import, that a cancelled import waits for its worker to stop at a between-files
checkpoint before the cancel propagates, and that every target of one import is
judged against the configuration installed when it started.

Every test that blocks the worker bounds the wait (``_Gate``): if the body ran
on the loop again, the block would hold the loop itself, the wait would time
out, and the test would fail on ``timed_out`` instead of hanging.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from memtomem.indexing import importers
from memtomem.indexing.engine import WriteTargetSnapshot
from memtomem.indexing.importers import import_notion, import_obsidian

_WAIT_S = 5.0


class _Gate:
    """Blocks the worker once, at the call it wraps, for at most ``_WAIT_S``."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.timed_out = False
        self.used = False
        self.thread: int | None = None

    def block(self) -> None:
        if self.used:
            return
        self.used = True
        self.thread = threading.get_ident()
        self.entered.set()
        if not self.release.wait(_WAIT_S):
            self.timed_out = True

    async def wait_entered(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, _WAIT_S), "worker never reached the gate"


def _notion_dir(tmp_path: Path, n: int = 3) -> Path:
    root = tmp_path / "notion-export"
    root.mkdir()
    for i in range(n):
        (root / f"Page {i} {i:032x}.md").write_text(f"# Page {i}\n\nbody {i}\n", encoding="utf-8")
    return root


def _notion_zip(tmp_path: Path, n: int = 3) -> Path:
    archive = tmp_path / "notion-export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for i in range(n):
            zf.writestr(f"Export/Page {i} {i:032x}.md", f"# Page {i}\n\nbody {i}\n")
    return archive


def _vault(tmp_path: Path, n: int = 3) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    for i in range(n):
        (root / f"note{i}.md").write_text(
            f"# Note {i}\n\n[[note{(i + 1) % n}]]\n", encoding="utf-8"
        )
    return root


_SOURCES = {
    "notion-dir": (_notion_dir, import_notion),
    "notion-zip": (_notion_zip, import_notion),
    "obsidian": (_vault, import_obsidian),
}


def _written(out: Path) -> list[Path]:
    return sorted(p for p in out.rglob("*.md")) if out.exists() else []


def _gate_first_write(monkeypatch: pytest.MonkeyPatch, gate: _Gate, *, fail: bool = False) -> None:
    real = importers.atomic_write_text

    def gated(path, text, mode=0o600, encoding="utf-8"):
        first = not gate.used
        gate.block()
        if fail and first:
            raise OSError("disk went away")
        return real(path, text, mode=mode, encoding=encoding)

    monkeypatch.setattr(importers, "atomic_write_text", gated)


# ── Off-loop witness ─────────────────────────────────────────────────────

_recorder: list[tuple[str, str, int]] | None = None
_hook_installed = False


def _audit(event: str, args: tuple) -> None:
    rec = _recorder
    if rec is None or event not in ("open", "os.scandir", "os.rename", "os.replace"):
        return
    if not args:
        return
    path = args[0]
    if isinstance(path, int) or path is None:
        return  # descriptor-only opens name no path
    try:
        rec.append((event, os.fsdecode(os.fspath(path)), threading.get_ident()))
    except TypeError:
        return


def _install_hook() -> None:
    global _hook_installed
    if not _hook_installed:
        # Audit hooks cannot be removed; this one is inert unless ``_recorder`` is set.
        sys.addaudithook(_audit)
        _hook_installed = True


def _under(path: str, root: Path) -> bool:
    return any(path == str(r) or path.startswith(str(r) + os.sep) for r in {root, root.resolve()})


@pytest.mark.parametrize("kind", sorted(_SOURCES))
async def test_import_io_runs_off_the_loop_thread(tmp_path, monkeypatch, kind):
    global _recorder
    make, importer = _SOURCES[kind]
    source = make(tmp_path)
    out = tmp_path / "out"
    loop_thread = threading.get_ident()

    member_reads: list[int] = []
    real_zip_open = zipfile.ZipFile.open

    def spy_open(self, name, *args, **kwargs):
        # A member read reuses the archive's descriptor and raises no audit event.
        member_reads.append(threading.get_ident())
        return real_zip_open(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy_open)
    _install_hook()
    events: list[tuple[str, str, int]] = []
    _recorder = events
    try:
        imported = await importer(source, out)
    finally:
        _recorder = None

    assert len(imported) == 3

    def threads(pred) -> list[int]:
        return [t for ev, p, t in events if pred(ev, p)]

    writes = threads(lambda ev, p: _under(p, out))
    groups: dict[str, list[int]] = {"destination writes": writes}
    if kind == "notion-zip":
        groups["archive open"] = threads(lambda ev, p: ev == "open" and _under(p, source))
        groups["member reads"] = member_reads
    else:
        groups["walk"] = threads(lambda ev, p: ev == "os.scandir" and _under(p, source))
        groups["source reads"] = threads(
            lambda ev, p: ev == "open" and _under(p, source) and p.endswith(".md")
        )
    for name, seen in groups.items():
        assert seen, f"no {name} observed — the witness is not seeing this operation"
        assert loop_thread not in seen, f"{name} ran on the event loop thread"


# ── Loop responsiveness ──────────────────────────────────────────────────


@pytest.mark.parametrize("kind", sorted(_SOURCES))
async def test_loop_keeps_running_while_an_import_is_blocked(tmp_path, monkeypatch, kind):
    make, importer = _SOURCES[kind]
    out = tmp_path / "out"
    gate = _Gate()
    _gate_first_write(monkeypatch, gate)

    task = asyncio.create_task(importer(make(tmp_path), out))
    try:
        await gate.wait_entered()
        ticks = 0
        for _ in range(20):
            await asyncio.sleep(0)
            ticks += 1
        assert ticks == 20
        assert not task.done(), "the import finished while its worker was still blocked"
        assert gate.thread != threading.get_ident()
    finally:
        gate.release.set()
        imported = await task
    assert not gate.timed_out, "the import body blocked the event loop"
    assert len(imported) == 3


# ── Cancellation ─────────────────────────────────────────────────────────


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("kind", sorted(_SOURCES))
async def test_cancel_waits_for_the_worker_and_stops_between_files(tmp_path, monkeypatch, kind):
    make, importer = _SOURCES[kind]
    out = tmp_path / "out"
    gate = _Gate()
    _gate_first_write(monkeypatch, gate)

    task = asyncio.create_task(importer(make(tmp_path), out))
    try:
        await gate.wait_entered()
        task.cancel()
        await _settle()
        assert not task.done(), "the cancel propagated while the worker was still writing"
    finally:
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not gate.timed_out
    # The in-flight file completes; the checkpoint stops the other two.
    assert len(_written(out)) == 1


async def test_second_cancel_during_the_drain_still_waits(tmp_path, monkeypatch):
    out = tmp_path / "out"
    gate = _Gate()
    _gate_first_write(monkeypatch, gate)

    task = asyncio.create_task(import_obsidian(_vault(tmp_path), out))
    try:
        await gate.wait_entered()
        task.cancel()
        await _settle()
        task.cancel()
        await _settle()
        assert not task.done()
    finally:
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not gate.timed_out
    assert len(_written(out)) == 1


async def test_shutdown_cancelling_every_task_still_waits_for_the_worker(tmp_path, monkeypatch):
    """``asyncio.run`` cancels every Task at shutdown, not only the importer's.

    A worker wrapped in a Task would be cancelled too and report done while its
    thread still wrote, ending the drain early.
    """
    out = tmp_path / "out"
    gate = _Gate()
    _gate_first_write(monkeypatch, gate)

    task = asyncio.create_task(import_obsidian(_vault(tmp_path), out))
    try:
        await gate.wait_entered()
        me = asyncio.current_task()
        others = [t for t in asyncio.all_tasks() if t is not me]
        assert task in others
        for t in others:
            t.cancel()
        await _settle()
        assert not task.done(), "the drain ended while the worker thread was still writing"
    finally:
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not gate.timed_out
    assert len(_written(out)) == 1


async def test_worker_error_after_a_cancel_is_retrieved_not_raised(tmp_path, monkeypatch, caplog):
    out = tmp_path / "out"
    gate = _Gate()
    _gate_first_write(monkeypatch, gate, fail=True)

    task = asyncio.create_task(import_obsidian(_vault(tmp_path), out))
    caplog.set_level(logging.ERROR, logger="asyncio")
    try:
        await gate.wait_entered()
        task.cancel()
        await _settle()
    finally:
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    del task
    gc.collect()
    await asyncio.sleep(0)
    assert not gate.timed_out
    assert "never retrieved" not in caplog.text
    assert _written(out) == []


async def test_worker_error_without_a_cancel_propagates(tmp_path, monkeypatch):
    gate = _Gate()
    gate.release.set()
    _gate_first_write(monkeypatch, gate, fail=True)

    with pytest.raises(OSError, match="disk went away"):
        await import_obsidian(_vault(tmp_path), tmp_path / "out")


def _gate_source_read(monkeypatch: pytest.MonkeyPatch, kind: str, source: Path, gate: _Gate):
    """Block the worker in the read of the *last* source file."""
    if kind == "notion-zip":
        real_zip_open = zipfile.ZipFile.open

        def gated_zip_open(self, name, *args, **kwargs):
            filename = getattr(name, "filename", name)
            if filename.endswith("Page 2 " + f"{2:032x}.md"):
                gate.block()
            return real_zip_open(self, name, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "open", gated_zip_open)
        return
    real_read_text = Path.read_text
    last = sorted(source.rglob("*.md"))[-1]

    def gated_read_text(self, *args, **kwargs):
        if self == last:
            gate.block()
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", gated_read_text)


@pytest.mark.parametrize("kind", sorted(_SOURCES))
async def test_cancel_during_a_read_writes_nothing_more(tmp_path, monkeypatch, kind):
    make, importer = _SOURCES[kind]
    source = make(tmp_path)
    out = tmp_path / "out"
    gate = _Gate()
    _gate_source_read(monkeypatch, kind, source, gate)
    mapped: list[bool] = []
    real_mapping = importers._build_notion_output_mapping

    def spy_mapping(sources):
        mapped.append(True)
        return real_mapping(sources)

    monkeypatch.setattr(importers, "_build_notion_output_mapping", spy_mapping)

    task = asyncio.create_task(importer(source, out))
    try:
        await gate.wait_entered()
        task.cancel()
        await _settle()
    finally:
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not gate.timed_out
    if kind == "obsidian":
        # Files before the last were already whole units when the cancel came.
        assert len(_written(out)) == 2
    else:
        # Notion reads every page before it maps or writes any of them.
        assert _written(out) == []
        assert mapped == [], "a cancel during the last read still went on to map the export"


# ── Config snapshot ──────────────────────────────────────────────────────


async def test_every_target_is_judged_against_the_config_at_dispatch(
    bm25_only_components, tmp_path, monkeypatch
):
    comp, mem_dir = bm25_only_components
    engine = comp.index_engine
    out = mem_dir / "_imported" / "obsidian"
    gate = _Gate()
    _gate_first_write(monkeypatch, gate)
    excluded: list[str] = []
    read_only: list[str] = []

    task = asyncio.create_task(
        import_obsidian(
            _vault(tmp_path),
            out,
            index_guard=engine,
            excluded_paths=excluded,
            read_only_paths=read_only,
        )
    )
    try:
        await gate.wait_entered()
        # What a concurrent tool call's config install does, on the loop.
        engine._config.exclude_patterns = ["**/_imported/**"]
        engine._config.read_only_memory_dirs = [mem_dir / "_imported"]
    finally:
        gate.release.set()
        imported = await task

    assert not gate.timed_out
    # The change is real: asked now, the live engine refuses these targets.
    assert all(engine.is_excluded(p) for p in imported)
    assert all(engine.is_read_only_source(p) for p in imported)
    # The import judged all three against the config installed when it started.
    assert len(imported) == 3
    assert excluded == []
    assert read_only == []


def test_snapshot_answers_like_the_live_engine(tmp_path):
    from memtomem.config import IndexingConfig
    from memtomem.indexing.engine import IndexEngine

    mem = tmp_path / "mem"
    proj = tmp_path / "proj"
    ro = tmp_path / "ro"
    for d in (mem, proj, ro, mem / "sub", proj / "sub"):
        d.mkdir(parents=True, exist_ok=True)
    # ``/sub/b.md`` is anchored: it matches only a path relative to an index
    # root, so a snapshot that lost its roots answers differently on it.
    config = IndexingConfig(
        memory_dirs=[mem],
        project_memory_dirs=[proj],
        read_only_memory_dirs=[ro],
        exclude_patterns=["**/secret*", "/sub/b.md"],
    )
    engine = IndexEngine.__new__(IndexEngine)
    engine._config = config
    snapshot = engine.write_target_snapshot()
    assert isinstance(snapshot, WriteTargetSnapshot)

    targets = [
        mem / "a.md",
        mem / "sub" / "b.md",
        mem / "sub" / "c.md",
        proj / "sub" / "b.md",
        tmp_path / "sub" / "b.md",
        mem / "secret-notes.md",
        mem / ".env",
        ro / "c.md",
        tmp_path / "elsewhere" / "d.md",
    ]
    excluded = [engine.is_excluded(t) for t in targets]
    protected = [engine.is_read_only_source(t) for t in targets]
    assert [snapshot.is_excluded(t) for t in targets] == excluded
    assert [snapshot.is_read_only_source(t) for t in targets] == protected
    # Non-vacuous: the matrix has both answers for both predicates, and the
    # anchored pattern excludes under each writable root tier but not elsewhere.
    assert set(excluded) == {True, False}
    assert set(protected) == {True, False}
    by_target = dict(zip(targets, excluded))
    assert by_target[mem / "sub" / "b.md"] is True
    assert by_target[proj / "sub" / "b.md"] is True
    assert by_target[mem / "sub" / "c.md"] is False
    assert by_target[tmp_path / "sub" / "b.md"] is False
