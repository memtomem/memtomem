"""Cross-process serialization of the memory-CRUD read-modify-write span
(issue #1587, follow-up to #1570).

#1570 held an in-process ``asyncio.Lock`` across each CRUD span — enough for
several agents sharing ONE MCP server, but a second server, the CLI, or
``memory-migrate`` still raced the read→rewrite→reindex window. #1587 holds the
cross-process sidecar (``async_file_lock``, level L2 of the lock order in
``context._atomic``) across the whole span too, and hoists the engine's own
sidecar acquire above ``_index_lock`` so a CRUD caller can reach ``index_file``
with ``lock_held=True`` instead of self-deadlocking.

Test groups:
* **A** — cross-process (``multiprocessing`` spawn, like ``test_config_write_lock``):
  the sidecar serializes real appends across processes; times out cleanly.
* **B** — lock ordering: the sidecar is acquired while ``_index_lock`` is free;
  ``lock_held=True`` and the #1566 parent-gone case skip it.
* **C** — timeout surfacing: a held sidecar makes a CRUD span / migrate return a
  friendly retryable error rather than blocking (also pins the Windows-safe
  in-process guard — two contenders on one loop serialize via layer 1).
* **D** — watcher: a timed-out reindex is re-queued, not dropped.
* **E** — sidecar-lockfile hygiene: ``.*.lock`` files are never indexed.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from helpers import StubCtx
from memtomem.context import _atomic as atomic_mod
from memtomem.context._atomic import _lock_path_for, async_file_lock, memory_lock_path
from memtomem.indexing import engine as engine_mod
from memtomem.indexing.engine import IndexEngine
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud

# spawn: uniform semantics across the CI matrix (Windows/macOS default), and the
# only context that genuinely gives distinct-process flock contention.
_CTX = mp.get_context("spawn")


# ----------------------------------------------------------------- helpers


def _locked_append(md_path_str: str, entry: str, q) -> None:
    """Locked read→append→write of one distinct line under ``async_file_lock``,
    with a widened read→write window so an unlocked version would reliably lose
    updates (mirrors ``test_config_write_lock._locked_add_section``). Runs in a
    child process, so serialization here comes purely from the cross-process
    flock — the in-process guard is per-process and cannot help across them."""

    async def run() -> None:
        path = Path(md_path_str)
        async with async_file_lock(_lock_path_for(path), timeout=20.0):
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            await asyncio.sleep(0.02)
            path.write_text(existing + entry + "\n", encoding="utf-8")

    asyncio.run(run())
    q.put(entry)


def _hold_sidecar(lock_path_str: str, ready_q, release_evt) -> None:
    """Hold the sidecar until signalled — stands in for another process owning
    the memory file's lock so same-file writers must time out."""

    async def run() -> None:
        async with async_file_lock(Path(lock_path_str), timeout=20.0):
            ready_q.put("acquired")
            await asyncio.to_thread(release_evt.wait, 30)

    asyncio.run(run())


# ============================================================ A. cross-process


def test_cross_process_appends_do_not_lose_updates(tmp_path: Path):
    """Positive pin: 8 processes each append a distinct line under the sidecar;
    all 8 survive. Without the lock the widened window loses updates."""
    md = tmp_path / "notes.md"
    entries = [f"entry-{i}" for i in range(8)]

    procs, queues = [], []
    for entry in entries:
        q = _CTX.Queue()
        p = _CTX.Process(target=_locked_append, args=(str(md), entry, q))
        queues.append(q)
        procs.append(p)
        p.start()

    for q in queues:
        assert q.get(timeout=30) in entries
    for p in procs:
        p.join(timeout=15)
        assert p.exitcode == 0

    survived = set(md.read_text(encoding="utf-8").split())
    assert survived == set(entries), (
        f"lost updates: expected all of {entries}, got {sorted(survived)}"
    )


def test_async_file_lock_uses_dot_prefixed_sidecar(tmp_path: Path):
    """The lock is a ``.{name}.lock`` sibling, never the data file — locking the
    data file wouldn't survive the ``os.replace`` inode swap."""

    async def run() -> None:
        md = tmp_path / "notes.md"
        async with async_file_lock(_lock_path_for(md), timeout=5.0):
            pass
        assert (tmp_path / ".notes.md.lock").exists()

    asyncio.run(run())


def test_async_file_lock_times_out_when_held_by_another_process(tmp_path: Path):
    """A separate process holding the sidecar makes an acquire raise
    ``TimeoutError`` (acquiring nothing) within the budget."""
    md = tmp_path / "notes.md"
    lock_path = _lock_path_for(md)

    ready_q = _CTX.Queue()
    release_evt = _CTX.Event()
    holder = _CTX.Process(target=_hold_sidecar, args=(str(lock_path), ready_q, release_evt))
    holder.start()
    try:
        assert ready_q.get(timeout=15) == "acquired"

        async def run() -> None:
            start = time.monotonic()
            with pytest.raises(TimeoutError):
                async with async_file_lock(lock_path, timeout=0.3):
                    pass
            # Bounded: gave up near the budget, did not hang on the holder.
            assert time.monotonic() - start < 5.0

        asyncio.run(run())
    finally:
        release_evt.set()
        holder.join(timeout=10)
        assert holder.exitcode == 0


# ============================================================ B. lock ordering


@pytest.mark.asyncio
async def test_index_file_acquires_sidecar_while_index_lock_free(bm25_only_components, monkeypatch):
    """The sidecar (L2) is acquired ABOVE ``_index_lock`` (L3): when the spy
    records the sidecar acquire, ``_index_lock`` must still be free. This is the
    reorder that removes the reverse-order cycle #1587 fixes."""
    comp, mem_dir = bm25_only_components
    src = mem_dir / "rule.md"
    src.write_text("## Rule\n\nbody.\n", encoding="utf-8")

    engine = comp.index_engine
    index_lock_held_at_acquire: list[bool] = []
    real = atomic_mod.async_file_lock

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def spy(lock_path, *, timeout):
        index_lock_held_at_acquire.append(engine._index_lock.locked())
        async with real(lock_path, timeout=timeout):
            yield

    monkeypatch.setattr(atomic_mod, "async_file_lock", spy)
    await engine.index_file(src.resolve())

    assert index_lock_held_at_acquire == [False], (
        "sidecar must be taken before _index_lock (L2 → L3), never while it is held"
    )


@pytest.mark.asyncio
async def test_index_file_lock_held_skips_sidecar(bm25_only_components, monkeypatch):
    """``lock_held=True`` skips the sidecar acquire entirely — the CRUD caller
    already holds it, and re-acquiring would self-deadlock."""
    comp, mem_dir = bm25_only_components
    src = mem_dir / "rule.md"
    src.write_text("## Rule\n\nbody.\n", encoding="utf-8")

    calls: list[Path] = []
    real = atomic_mod.async_file_lock

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def spy(lock_path, *, timeout):
        calls.append(lock_path)
        async with real(lock_path, timeout=timeout):
            yield

    monkeypatch.setattr(atomic_mod, "async_file_lock", spy)
    await comp.index_engine.index_file(src.resolve(), force=True, lock_held=True)

    assert calls == [], "lock_held=True must not acquire the sidecar"


@pytest.mark.asyncio
async def test_index_file_parent_gone_skips_sidecar_without_mkdir(
    bm25_only_components, monkeypatch, tmp_path
):
    """#1566: when the parent dir is gone, the sidecar is skipped so we never
    ``mkdir``-resurrect the directory the user removed."""
    comp, _mem_dir = bm25_only_components
    missing = tmp_path / "gone" / "orphan.md"  # parent 'gone/' never created

    calls: list[Path] = []
    real = atomic_mod.async_file_lock

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def spy(lock_path, *, timeout):
        calls.append(lock_path)
        async with real(lock_path, timeout=timeout):
            yield

    monkeypatch.setattr(atomic_mod, "async_file_lock", spy)
    await comp.index_engine.index_file(missing)  # delete-by-source pass

    assert calls == [], "parent-gone path must skip the sidecar"
    assert not (tmp_path / "gone").exists(), "sidecar acquire resurrected the deleted parent dir"


@pytest.mark.asyncio
async def test_stream_acquires_sidecar_while_index_lock_free(bm25_only_components, monkeypatch):
    """``index_path_stream`` uses the same L2 → L3 order as ``index_file``
    (via ``_index_file_locked``): the sidecar acquire happens while
    ``_index_lock`` is free (#1574 item 6). Before that fix the stream
    bypassed both locks entirely — ``calls`` would be empty."""
    comp, mem_dir = bm25_only_components
    src = mem_dir / "rule.md"
    src.write_text("## Rule\n\nbody.\n", encoding="utf-8")

    engine = comp.index_engine
    index_lock_held_at_acquire: list[bool] = []
    real = atomic_mod.async_file_lock

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def spy(lock_path, *, timeout):
        index_lock_held_at_acquire.append(engine._index_lock.locked())
        async with real(lock_path, timeout=timeout):
            yield

    monkeypatch.setattr(atomic_mod, "async_file_lock", spy)
    async for _event in engine.index_path_stream(src, recursive=False):
        pass

    assert index_lock_held_at_acquire == [False], (
        "the stream must take the sidecar (L2) before _index_lock (L3), once per file"
    )


@pytest.mark.asyncio
async def test_stream_sidecar_timeout_folds_into_errors_and_continues(
    bm25_only_components, monkeypatch
):
    """A held sidecar on one file must not abort the stream: that file's
    ``TimeoutError`` lands in ``complete.errors`` (no new event type) and
    the remaining files still index (#1574 item 6)."""
    comp, mem_dir = bm25_only_components
    stuck = mem_dir / "a-stuck.md"
    ok = mem_dir / "b-ok.md"
    stuck.write_text("## Stuck\n\nheld.\n", encoding="utf-8")
    ok.write_text("## Ok\n\nfree.\n", encoding="utf-8")

    monkeypatch.setattr(atomic_mod, "_MEMORY_SIDECAR_LOCK_BUDGET_S", 0.2)

    held = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with async_file_lock(_lock_path_for(stuck.resolve()), timeout=5.0):
            held.set()
            await release.wait()

    holder_task = asyncio.create_task(holder())
    await held.wait()
    try:
        events = [e async for e in comp.index_engine.index_path_stream(mem_dir, recursive=True)]
    finally:
        release.set()
        await holder_task

    complete = next(e for e in events if e["type"] == "complete")
    assert len(complete["errors"]) == 1, f"expected 1 timeout error, got {complete['errors']}"
    assert "a-stuck.md" in complete["errors"][0]
    # A sidecar budget overrun is transient, so it must also be reported as
    # retryable — ``mm index`` drives its hook retry off this field (#2105).
    assert complete["retryable_errors"] == complete["errors"]
    # The other file still indexed — the stream continued past the timeout.
    sources = {p.name for p in await comp.storage.get_all_source_files()}
    assert "b-ok.md" in sources
    assert "a-stuck.md" not in sources


# ============================================================ C. timeout surface


@pytest.mark.asyncio
async def test_mem_edit_times_out_when_sidecar_held(bm25_only_components, monkeypatch):
    """A held sidecar makes ``mem_edit`` return a friendly retryable error
    instead of blocking. Holding the lock on THIS loop also exercises the
    in-process (layer-1) guard, which serializes same-process handlers
    independently of what the file lock does (Codex #1587 review)."""
    comp, mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)

    await memory_crud.mem_add(content="Alpha body", title="Alpha", file="d.md", ctx=ctx)
    f = mem_dir / "d.md"
    (alpha,) = sorted(
        await comp.storage.list_chunks_by_source(f.resolve()),
        key=lambda c: c.metadata.start_line,
    )

    monkeypatch.setattr(atomic_mod, "_CRUD_SIDECAR_LOCK_BUDGET_S", 0.2)
    sidecar = _lock_path_for(f.resolve())

    async with async_file_lock(sidecar, timeout=5.0):
        out = await memory_crud.mem_edit(chunk_id=str(alpha.id), new_content="NEW BODY", ctx=ctx)

    assert "locked by another process" in out
    # File untouched — the edit never ran.
    assert "Alpha body" in f.read_text(encoding="utf-8")
    assert "NEW BODY" not in f.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_mem_add_times_out_when_sidecar_held(bm25_only_components, monkeypatch):
    """``mem_add`` surfaces the same retryable error under a held sidecar."""
    comp, mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)

    monkeypatch.setattr(atomic_mod, "_CRUD_SIDECAR_LOCK_BUDGET_S", 0.2)
    target = (mem_dir / "d.md").resolve()
    (mem_dir / "d.md").write_text("## Seed\n\nseed.\n", encoding="utf-8")

    async with async_file_lock(_lock_path_for(target), timeout=5.0):
        out = await memory_crud.mem_add(content="new entry", title="New", file="d.md", ctx=ctx)

    assert "locked by another process" in out


# ==================================================== B2. bulk index_path (#2105)


@pytest.mark.asyncio
async def test_index_path_acquires_sidecar_per_file_while_index_lock_free(
    bm25_only_components, monkeypatch
):
    """The bulk (non-stream) path takes the L2 sidecar once per file, with
    ``_index_lock`` free at every acquire (#2105).

    Before the fix ``_index_path_inner`` called ``_index_file`` directly under
    a run-wide ``_index_lock``: ``lock_paths`` would be empty, so a second
    process could move a file's chunks between this run's namespace
    resolution and its commit. The ``[False, False]`` half is the ordering
    invariant — L2 is never acquired while L3 is held.
    """
    comp, mem_dir = bm25_only_components
    (mem_dir / "a.md").write_text("## A\n\nbody.\n", encoding="utf-8")
    (mem_dir / "b.md").write_text("## B\n\nbody.\n", encoding="utf-8")

    engine = comp.index_engine
    lock_paths: list[Path] = []
    index_lock_held_at_acquire: list[bool] = []
    real = atomic_mod.async_file_lock

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def spy(lock_path, *, timeout):
        lock_paths.append(Path(lock_path))
        index_lock_held_at_acquire.append(engine._index_lock.locked())
        async with real(lock_path, timeout=timeout):
            yield

    monkeypatch.setattr(atomic_mod, "async_file_lock", spy)
    await engine.index_path(mem_dir, recursive=True)

    assert sorted(p.name for p in lock_paths) == [".a.md.lock", ".b.md.lock"], (
        f"bulk index must take one sidecar per file, got {[p.name for p in lock_paths]}"
    )
    assert index_lock_held_at_acquire == [False, False], (
        "sidecar must be taken before _index_lock (L2 -> L3), never while it is held"
    )


@pytest.mark.asyncio
async def test_index_path_sidecar_timeout_folds_into_errors_and_continues(
    bm25_only_components, monkeypatch
):
    """A sidecar held by another *process* makes the bulk run skip that file
    with a retryable error and index the rest (#2105).

    The cross-process half of the guarantee: unlike the same-loop holder in
    the stream test above, this contends on the real ``portalocker`` flock,
    which is what a second ``mm`` or the web/MCP server actually holds.
    """
    comp, mem_dir = bm25_only_components
    stuck = mem_dir / "a-stuck.md"
    ok = mem_dir / "b-ok.md"
    stuck.write_text("## Stuck\n\nheld.\n", encoding="utf-8")
    ok.write_text("## Ok\n\nfree.\n", encoding="utf-8")

    monkeypatch.setattr(atomic_mod, "_MEMORY_SIDECAR_LOCK_BUDGET_S", 0.2)

    ready_q = _CTX.Queue()
    release_evt = _CTX.Event()
    holder = _CTX.Process(
        target=_hold_sidecar,
        args=(str(_lock_path_for(stuck.resolve())), ready_q, release_evt),
    )
    holder.start()
    try:
        assert ready_q.get(timeout=15) == "acquired"

        start = time.monotonic()
        stats = await comp.index_engine.index_path(mem_dir, recursive=True)
        # Bounded by the budget, not by the holder: a regression to a blocking
        # acquire would hang here instead of failing.
        assert time.monotonic() - start < 5.0
    finally:
        release_evt.set()
        holder.join(timeout=10)
        assert holder.exitcode == 0

    assert len(stats.errors) == 1, f"expected 1 timeout error, got {stats.errors}"
    assert "a-stuck.md" in stats.errors[0]
    assert "could not acquire" in stats.errors[0]
    assert stats.retryable_errors == stats.errors, (
        "a sidecar budget overrun is transient and must be reported as retryable"
    )

    sources = {p.name for p in await comp.storage.get_all_source_files()}
    assert "b-ok.md" in sources
    assert "a-stuck.md" not in sources


@pytest.mark.asyncio
async def test_two_engines_over_one_file_serialize_on_the_sidecar(bm25_only_components):
    """Two independent ``IndexEngine`` instances over one file do not overlap.

    This is the in-process half of the issue's "two engines over one file"
    ask, and it is the sharper half: the engines have *distinct*
    ``_index_lock`` objects, so L3 cannot be what serializes them — only the
    sidecar can (``_atomic._intra_async_locks`` is module-global, keyed by
    ``(lock_path, loop)``). The cross-process half is
    ``test_index_path_sidecar_timeout_folds_into_errors_and_continues``,
    which contends on the real flock. Two full component stacks over one
    sqlite file would mostly measure sqlite's own writer lock instead.
    """
    comp, mem_dir = bm25_only_components
    (mem_dir / "shared.md").write_text("## Shared\n\nbody.\n", encoding="utf-8")

    other = IndexEngine(comp.storage, comp.embedder, comp.config.indexing)
    assert other._index_lock is not comp.index_engine._index_lock

    trace: list[str] = []

    def _instrument(engine: IndexEngine, tag: str) -> None:
        orig = engine._index_file

        async def wrapped(fp, force=False, namespace=None, **kwargs):
            trace.append(f"enter:{tag}")
            try:
                await asyncio.sleep(0.05)
                return await orig(fp, force, namespace=namespace)
            finally:
                trace.append(f"exit:{tag}")

        engine._index_file = wrapped  # type: ignore[method-assign]

    _instrument(comp.index_engine, "one")
    _instrument(other, "two")

    await asyncio.gather(
        comp.index_engine.index_path(mem_dir, recursive=True),
        other.index_path(mem_dir, recursive=True),
    )

    assert len(trace) == 4, f"expected two enter/exit pairs, got {trace}"
    assert trace[0].startswith("enter:") and trace[1] == trace[0].replace("enter", "exit"), (
        f"the two engines interleaved on one file: {trace}"
    )


@pytest.mark.asyncio
async def test_index_path_waits_for_a_crud_span_holding_the_sidecar(bm25_only_components):
    """A real CRUD-shaped span — sidecar held across the whole
    read→rewrite→reindex window, reindexing via ``index_file(lock_held=True)``
    — blocks the bulk run on that file only (#2105).

    This is the branch the two halves of the design meet on: the span holds
    L2 and enters at L3, while the bulk worker holds no L3 at all, so L2 is
    the *only* thing that can keep their ``_index_file`` bodies apart. The
    span's own reindex must not be starved either — it is exempt from the
    engine-wide file slot precisely so bulk workers queued on its sidecar
    cannot deadlock it.
    """
    comp, mem_dir = bm25_only_components
    held_file = mem_dir / "a-held.md"
    free_file = mem_dir / "b-free.md"
    held_file.write_text("## Held\n\nbody.\n", encoding="utf-8")
    free_file.write_text("## Free\n\nbody.\n", encoding="utf-8")

    engine = comp.index_engine
    inflight: list[str] = []
    overlaps: list[tuple[str, ...]] = []
    started: list[str] = []
    orig = engine._index_file

    async def wrapped(fp, force=False, namespace=None, **kwargs):
        name = Path(fp).name
        started.append(name)
        inflight.append(name)
        if len(inflight) > 1:
            overlaps.append(tuple(inflight))
        try:
            await asyncio.sleep(0)
            return await orig(fp, force, namespace=namespace)
        finally:
            inflight.remove(name)

    engine._index_file = wrapped  # type: ignore[method-assign]

    acquired = asyncio.Event()
    release = asyncio.Event()

    async def crud_span() -> None:
        # The CRUD contract: hold L2 for the whole span, then reindex with
        # ``lock_held=True`` so the engine does not re-acquire the sidecar.
        async with async_file_lock(_lock_path_for(held_file.resolve()), timeout=5.0):
            acquired.set()
            await release.wait()
            held_file.write_text("## Held\n\nrewritten.\n", encoding="utf-8")
            await engine.index_file(held_file, lock_held=True)

    span_task = asyncio.create_task(crud_span())
    await acquired.wait()

    run = asyncio.create_task(engine.index_path(mem_dir, recursive=True))
    for _ in range(20):
        await asyncio.sleep(0)
    assert started == ["b-free.md"], (
        f"the held file must not be indexed while the span holds its sidecar: {started}"
    )

    release.set()
    await span_task
    stats = await run

    assert stats.errors == ()
    assert sorted(set(started)) == ["a-held.md", "b-free.md"]
    assert not any("a-held.md" in pair and len(set(pair)) == 1 for pair in overlaps), (
        f"the span and the bulk worker overlapped on one file: {overlaps}"
    )


@pytest.mark.asyncio
async def test_lock_held_reindex_is_not_starved_by_a_saturated_semaphore(
    bm25_only_components, monkeypatch
):
    """The ``lock_held=True`` exemption from the file-concurrency slot is what
    keeps the lock order acyclic — pin it under saturation (#2105).

    Every slot is held by a worker blocked on the CRUD span's sidecar. If a
    ``lock_held=True`` reindex also queued for a slot it could never get one
    (the holders only release once the span releases, and the span cannot
    finish without reindexing), so this would deadlock rather than fail an
    assertion — hence the ``wait_for``. ``_FILE_CONCURRENCY`` is monkeypatched
    to 1 so "saturated" is one parked worker, not eight.
    """
    comp, mem_dir = bm25_only_components
    target = mem_dir / "held.md"
    target.write_text("## Held\n\nbody.\n", encoding="utf-8")

    monkeypatch.setattr(engine_mod, "_FILE_CONCURRENCY", 1)
    engine = comp.index_engine

    async with async_file_lock(_lock_path_for(target.resolve()), timeout=5.0):
        # Saturate the single slot with a bulk run that will park on the
        # sidecar we hold.
        bulk = asyncio.create_task(engine.index_path(mem_dir, recursive=True))
        for _ in range(20):
            await asyncio.sleep(0)
        assert engine._file_semaphore().locked(), "the bulk worker should hold the only slot"

        target.write_text("## Held\n\nrewritten.\n", encoding="utf-8")
        stats = await asyncio.wait_for(engine.index_file(target, lock_held=True), timeout=5.0)
        assert stats.total_files == 1

    await bulk


@pytest.mark.requires_symlinks
@pytest.mark.asyncio
async def test_crud_span_through_an_alias_still_excludes_the_indexer(
    bm25_only_components, monkeypatch
):
    """A CRUD span reaching a file through a symlink alias must still exclude a
    bulk run reaching the same file through its target (#2130).

    Both sides now key the sidecar with ``memory_lock_path``, which resolves —
    so the span's ``.alias.md.lock`` and the indexer's key are the same
    lockfile. Before the fix the span held ``.alias.md.lock`` while the engine
    held ``.target.md.lock``: two different files, no exclusion, and since
    #2105 the bulk path holds no ``_index_lock`` to fall back on, so their
    ``_index_file`` bodies could overlap outright.
    """
    comp, mem_dir = bm25_only_components
    target = mem_dir / "target.md"
    target.write_text("## Target\n\nbody.\n", encoding="utf-8")
    alias = mem_dir / "alias.md"
    alias.symlink_to(target)

    engine = comp.index_engine
    # ``started`` records ENTRIES, not what happens to be in flight at a
    # sampling instant: the pre-fix leak is a file that runs *and finishes*
    # inside the span, which an in-flight snapshot misses entirely.
    started: list[str] = []
    inflight: list[str] = []
    overlaps: list[tuple[str, ...]] = []
    orig = engine._index_file

    async def wrapped(fp, force=False, namespace=None, **kwargs):
        started.append(Path(fp).name)
        inflight.append(Path(fp).name)
        if len(inflight) > 1:
            overlaps.append(tuple(inflight))
        try:
            await asyncio.sleep(0)
            return await orig(fp, force, namespace=namespace)
        finally:
            inflight.remove(Path(fp).name)

    engine._index_file = wrapped  # type: ignore[method-assign]

    acquired = asyncio.Event()
    release = asyncio.Event()

    async def crud_span_via_alias() -> None:
        # The span keys on the ALIAS — the shape every CRUD caller has.
        async with async_file_lock(memory_lock_path(alias), timeout=5.0):
            acquired.set()
            await release.wait()

    span = asyncio.create_task(crud_span_via_alias())
    await acquired.wait()

    # Wait for BOTH of the engine's acquires to be attempted rather than
    # counting scheduler turns. One event would be satisfied by whichever file
    # reaches the lock first — under the broken keying that is the alias, which
    # blocks correctly, leaving the target's leak still ahead of the assertion.
    attempted: list[Path] = []
    both_attempted = asyncio.Event()
    real = atomic_mod.async_file_lock

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def spy(lock_path, *, timeout):
        attempted.append(Path(lock_path))
        if len(attempted) >= 2:
            both_attempted.set()
        async with real(lock_path, timeout=timeout):
            yield

    monkeypatch.setattr(atomic_mod, "async_file_lock", spy)

    run = asyncio.create_task(engine.index_path(mem_dir, recursive=True))
    await asyncio.wait_for(both_attempted.wait(), timeout=5.0)
    assert started == [], (
        "the indexer worked the file while a CRUD span held its alias's sidecar: "
        f"{started} — the two sides keyed different lockfiles"
    )
    # Both spellings asked for the SAME lockfile — the one the span holds.
    assert set(attempted) == {memory_lock_path(target)}, (
        f"engine keyed more than one sidecar for one physical file: {sorted(set(attempted))}"
    )

    release.set()
    await span
    stats = await run

    assert stats.errors == ()
    # Both spellings of one physical file share one sidecar, so the walk works
    # them one at a time even though they are two entries in the file set.
    assert sorted(started) == ["alias.md", "target.md"]
    assert overlaps == [], f"two writers overlapped on one physical file: {overlaps}"


@pytest.mark.asyncio
async def test_index_path_parent_gone_falls_back_to_index_lock(
    bm25_only_components, monkeypatch, tmp_path
):
    """#1566 under the bulk path: a file whose parent vanished after discovery
    skips the sidecar (never mkdir-resurrecting the dir) and takes
    ``_index_lock`` instead, so ``engine_serialized=False`` never leaves a
    file unlocked (#2105)."""
    comp, mem_dir = bm25_only_components
    missing = tmp_path / "gone" / "orphan.md"  # parent 'gone/' never created

    engine = comp.index_engine
    monkeypatch.setattr(
        engine, "discover_indexable_files", lambda *a, **kw: [missing], raising=True
    )

    calls: list[Path] = []
    real = atomic_mod.async_file_lock

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def spy(lock_path, *, timeout):
        calls.append(Path(lock_path))
        async with real(lock_path, timeout=timeout):
            yield

    monkeypatch.setattr(atomic_mod, "async_file_lock", spy)

    index_lock_held: list[bool] = []
    orig = engine._index_file

    async def wrapped(fp, force=False, namespace=None, **kwargs):
        index_lock_held.append(engine._index_lock.locked())
        return await orig(fp, force, namespace=namespace)

    engine._index_file = wrapped  # type: ignore[method-assign]

    await engine.index_path(mem_dir, recursive=True)

    assert calls == [], "parent-gone path must skip the sidecar"
    assert not (tmp_path / "gone").exists(), "sidecar acquire resurrected the deleted parent dir"
    assert index_lock_held == [True], "the sidecar-skipping bulk file must still hold _index_lock"


# ============================================================ D. watcher requeue


@pytest.mark.asyncio
async def test_watcher_reindex_returns_path_on_timeout(monkeypatch):
    """``_reindex`` returns the path (not ``None``) when the reindex times out
    on the sidecar, so the caller can retry it — the change is not lost."""
    from memtomem.config import IndexingConfig
    from memtomem.indexing.watcher import FileWatcher

    class _Engine:
        async def index_file(self, path):
            raise TimeoutError("sidecar held")

    watcher = FileWatcher(_Engine(), IndexingConfig(memory_dirs=[]))
    result = await watcher._reindex(Path("/some/notes.md"))
    assert result == Path("/some/notes.md")


@pytest.mark.asyncio
async def test_watcher_flush_batch_requeues_only_timed_out(monkeypatch):
    """``_flush_batch`` returns exactly the files whose reindex timed out (to be
    retried next window) and drops the ones that succeeded."""
    from memtomem.config import IndexingConfig
    from memtomem.indexing.watcher import FileWatcher

    ok = Path("/mem/ok.md")
    stuck = Path("/mem/stuck.md")

    class _Engine:
        async def index_file(self, path):
            if path == stuck:
                raise TimeoutError("sidecar held")

            class _Stats:
                indexed_chunks = 1
                skipped_chunks = 0
                deleted_chunks = 0

            return _Stats()

    watcher = FileWatcher(_Engine(), IndexingConfig(memory_dirs=[]))
    retry = await watcher._flush_batch({ok, stuck})
    assert retry == {stuck}


# ============================================================ E. sidecar hygiene


@pytest.mark.asyncio
async def test_sidecar_lockfiles_are_not_indexed(bm25_only_components):
    """A ``.{name}.md.lock`` sidecar living beside memory files is never picked
    up by a directory index — only the real markdown gets chunks.

    Since #2105 the bulk run creates such a sidecar for every file it visits,
    so this exclusion is load-bearing on every directory index, not only where
    a CRUD span happened to leave one behind.
    """
    comp, mem_dir = bm25_only_components
    (mem_dir / "real.md").write_text("## Real\n\nbody.\n", encoding="utf-8")
    # A sidecar lock as ``async_file_lock``/migrate leave behind.
    (mem_dir / ".real.md.lock").write_text("", encoding="utf-8")

    await comp.index_engine.index_path(mem_dir, recursive=True)

    sources = {p.name for p in await comp.storage.get_all_source_files()}
    assert "real.md" in sources
    assert not any(name.endswith(".lock") for name in sources), (
        f"a sidecar lockfile was indexed: {sorted(sources)}"
    )
