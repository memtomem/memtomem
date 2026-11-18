"""Indexing a file outside every memory directory creates nothing beside it.

``index_file`` accepts any path, and a namespace rename or assignment locks
whatever source its rows name. Both used to create ``.<name>.lock`` beside the
source: an empty file in a directory memtomem keeps nothing in, and, because
the create follows a link, an empty file wherever a link planted at that name
points.

For a source ``IndexEngine.is_managed_source`` answers no for, those two now
take a lock in the per-user runtime directory, then the adjacent sidecar only
if some writer already left one. The first group of tests is that the source's
directory is left alone; the second that a memory directory, the upload
directory and a read-only root keep the sidecar they had; the rest is what each
of the two locks still excludes and what the pair, by design, does not.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import stat
import sys
import unicodedata
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from helpers import set_home
from memtomem._runtime_paths import RuntimeDirValidationError, runtime_dir, validate_runtime_dir
from memtomem.context import _atomic
from memtomem.context._atomic import (
    _file_lock,
    async_detached_source_lock,
    async_existing_memory_file_lock,
    async_file_lock,
    detached_source_lock_key,
    memory_lock_path,
)
from memtomem.errors import NamespaceMutationBusyError
from memtomem.services import namespace_management
from memtomem.source_provenance import UPLOAD_DIR
from memtomem.storage.base import NamespaceChunkCandidate, NamespaceRenameResult

BODY = "## Alpha\n\nAlpha body\n"
OPERATIONS = ("index", "assign", "rename")

posix_modes = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permission bits that bind the caller",
)


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows without the right
        pytest.skip(f"cannot create a symlink here: {exc}")


async def _index(comp, source: Path, *, force: bool = False):
    stats = await comp.index_engine.index_file(source, ingress_guarded=True, force=force)
    assert not stats.errors, stats.errors
    return stats


async def _run(comp, operation: str, source: Path):
    """One of the three operations that lock ``source``; the two namespace ones move its rows."""
    managed = comp.index_engine.is_managed_source
    if operation == "index":
        return await _index(comp, source, force=True)
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    if operation == "assign":
        result = await namespace_management.assign_namespace(
            comp.storage,
            f"moved-{uuid4().hex[:8]}",
            is_managed_source=managed,
            source_filter=str(source),
        )
        assert result.chunks_moved == 1
        return result
    result = await namespace_management.rename_namespace(
        comp.storage,
        chunk.metadata.namespace,
        f"moved-{uuid4().hex[:8]}",
        is_managed_source=managed,
    )
    assert result.chunks_moved == 1
    return result


@pytest.fixture
async def host_source(bm25_only_components, tmp_path):
    """A file outside every index root, in a directory that holds nothing else."""
    comp, _mem_dir = bm25_only_components
    host = tmp_path / "host"
    host.mkdir()
    source = host / "note.md"
    source.write_text(BODY, encoding="utf-8")
    assert not comp.index_engine.is_managed_source(source)
    return comp, source


@pytest.fixture
def short_budgets(monkeypatch):
    """Make a held lock a quick refusal instead of a ten-second wait."""
    monkeypatch.setattr(_atomic, "_MEMORY_SIDECAR_LOCK_BUDGET_S", 0.3)
    monkeypatch.setattr(namespace_management, "_NAMESPACE_MUTATION_BUDGET_S", 0.3)


async def _assert_waits(comp, operation: str, source: Path) -> None:
    """The operation is turned away by a lock somebody else holds."""
    if operation == "index":
        with pytest.raises(TimeoutError):
            await comp.index_engine.index_file(source, ingress_guarded=True, force=True)
    else:
        with pytest.raises(NamespaceMutationBusyError):
            await _run(comp, operation, source)


# --- the source's directory is left alone -----------------------------------


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_an_unmanaged_source_gets_no_sidecar(host_source, operation):
    comp, source = host_source
    await _index(comp, source)
    assert _names(source.parent) == ["note.md"]

    await _run(comp, operation, source)

    assert _names(source.parent) == ["note.md"]
    assert source.read_text(encoding="utf-8") == BODY


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_a_link_planted_at_the_sidecar_name_is_not_followed(host_source, tmp_path, operation):
    """The create used to follow it and leave an empty file at its target."""
    comp, source = host_source
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    planted = source.parent / ".note.md.lock"
    _symlink_or_skip(planted, elsewhere / "created-by-index")

    await _index(comp, source)
    await _run(comp, operation, source)

    assert _names(elsewhere) == []
    assert planted.is_symlink()


async def test_a_link_to_a_file_somebody_holds_does_not_hold_up_the_index(
    host_source, tmp_path, short_budgets
):
    """Not followed means not locked through either: the target is nobody's sidecar."""
    comp, source = host_source
    held = tmp_path / "held-by-someone.lock"
    _symlink_or_skip(source.parent / ".note.md.lock", held)

    with _file_lock(held):
        await _index(comp, source)


@posix_modes
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_a_source_in_a_directory_that_cannot_be_written_is_indexed(host_source, operation):
    """Creating the sidecar there failed with ``PermissionError`` before the file was read."""
    comp, source = host_source
    os.chmod(source.parent, 0o555)
    try:
        await _index(comp, source)
        await _run(comp, operation, source)
        assert _names(source.parent) == ["note.md"]
    finally:
        os.chmod(source.parent, 0o755)


async def test_a_walk_over_an_unmanaged_directory_leaves_no_sidecars(host_source):
    """The bulk run reaches the lock through its own fan-out, with the walk's memo of the roots."""
    comp, source = host_source
    nested = source.parent / "nested"
    nested.mkdir()
    for directory in (source.parent, nested):
        for i in range(3):
            (directory / f"more{i}.md").write_text(BODY, encoding="utf-8")

    stats = await comp.index_engine.index_path(source.parent, recursive=True, path_scope="explicit")

    assert not stats.errors, stats.errors
    assert stats.total_files == 7
    assert not list(source.parent.rglob(".*.lock"))


async def test_a_walk_asks_about_each_directory_once(bm25_only_components, monkeypatch):
    """The predicate resolves every configured root per call; a walk must not pay that per file."""
    comp, mem_dir = bm25_only_components
    nested = mem_dir / "nested"
    nested.mkdir()
    for directory in (mem_dir, nested):
        for i in range(5):
            (directory / f"note{i}.md").write_text(BODY, encoding="utf-8")
    engine = comp.index_engine
    asked: list[Path] = []
    real = engine.is_managed_source

    def counting(path, stored=None):
        asked.append(Path(path))
        return real(path, stored)

    monkeypatch.setattr(engine, "is_managed_source", counting)

    stats = await engine.index_path(mem_dir, recursive=True)

    assert not stats.errors, stats.errors
    assert stats.total_files == 10
    assert sorted(asked) == sorted([mem_dir.resolve(), nested.resolve()])
    assert len(list(mem_dir.rglob(".*.lock"))) == 10


async def test_a_root_removed_mid_walk_is_judged_afresh(bm25_only_components, tmp_path):
    """The walk's memo stands only while the configured roots are the ones it was built from."""
    from memtomem.indexing.engine import IndexRoots

    comp, mem_dir = bm25_only_components
    engine = comp.index_engine
    memo = IndexRoots(engine._config.all_index_roots())
    assert engine._source_dir_is_managed(mem_dir.resolve(), memo) is True

    engine._config.memory_dirs = [tmp_path / "another"]

    assert engine._source_dir_is_managed(mem_dir.resolve(), memo) is False


# --- the directories memtomem keeps memory files in are locked as before ------


async def _assert_sidecar_from_each_operation(comp, source: Path) -> None:
    sidecar = memory_lock_path(source)
    assert comp.index_engine.is_managed_source(source)
    for operation in OPERATIONS:
        await _index(comp, source)
        sidecar.unlink(missing_ok=True)
        await _run(comp, operation, source)
        assert sidecar.is_file(), operation
    detached = runtime_dir() / _atomic._DETACHED_LOCK_DIRNAME
    assert not detached.exists(), "a managed source never takes the detached lock"


async def test_a_memory_directory_source_keeps_its_sidecar(bm25_only_components):
    comp, mem_dir = bm25_only_components
    source = mem_dir / "note.md"
    source.write_text(BODY, encoding="utf-8")

    await _assert_sidecar_from_each_operation(comp, source)


async def test_an_uploaded_source_keeps_its_sidecar(bm25_only_components, tmp_path, monkeypatch):
    comp, _mem_dir = bm25_only_components
    set_home(monkeypatch, tmp_path / "home")
    upload_dir = UPLOAD_DIR.expanduser()
    assert upload_dir.is_relative_to(tmp_path), "the upload directory must be the sandboxed one"
    upload_dir.mkdir(parents=True)
    source = upload_dir / "note.md"
    source.write_text(BODY, encoding="utf-8")

    await _assert_sidecar_from_each_operation(comp, source)


async def test_a_read_only_root_source_keeps_its_sidecar(bm25_only_components, tmp_path):
    """Indexing a read-only root still writes its sidecar there; that is #2501, unchanged here."""
    comp, _mem_dir = bm25_only_components
    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "note.md"
    source.write_text(BODY, encoding="utf-8")
    comp.config.indexing.read_only_memory_dirs = [vault]

    await _assert_sidecar_from_each_operation(comp, source)


# --- a sidecar that is already there is still the lock ------------------------


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_an_existing_sidecar_held_elsewhere_turns_the_operation_away(
    host_source, short_budgets, operation
):
    """Every other acquirer of this file takes its sidecar; one that exists keeps excluding."""
    comp, source = host_source
    await _index(comp, source)
    sidecar = memory_lock_path(source)
    sidecar.touch()
    inode = sidecar.stat().st_ino

    with _file_lock(sidecar):
        await _assert_waits(comp, operation, source)
    await _run(comp, operation, source)

    assert sidecar.stat().st_ino == inode, "the sidecar is locked in place, never replaced"
    assert _names(source.parent) == [".note.md.lock", "note.md"]


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_the_detached_lock_held_elsewhere_turns_the_operation_away(
    host_source, short_budgets, operation
):
    """With no sidecar, the detached lock is what the two functions contend on."""
    comp, source = host_source
    await _index(comp, source)
    detached = _atomic._detached_lock_path(source)
    assert detached.is_file(), "the first index must have taken the detached lock"

    with _file_lock(detached):
        await _assert_waits(comp, operation, source)
    await _run(comp, operation, source)

    assert _names(source.parent) == ["note.md"]


def _hold_detached_in_child(source: str, ready, release) -> None:
    """Process target: take the detached lock the way the engine does, from the data path."""

    async def hold() -> None:
        async with async_detached_source_lock(Path(source), timeout=5):
            ready.send(True)
            await asyncio.to_thread(release.recv)

    asyncio.run(hold())


async def test_another_process_computes_the_same_detached_lock(host_source, short_budgets):
    """Two processes of one user agree on the file without sharing anything but the path."""
    comp, source = host_source
    mp = multiprocessing.get_context("spawn")
    ready_parent, ready_child = mp.Pipe(duplex=False)
    release_child, release_parent = mp.Pipe(duplex=False)
    process = mp.Process(
        target=_hold_detached_in_child, args=(str(source), ready_child, release_child)
    )
    process.start()
    ready_child.close()
    release_child.close()
    try:
        assert ready_parent.poll(30), "child did not take the detached lock"
        assert ready_parent.recv() is True

        await _assert_waits(comp, "index", source)
    finally:
        if process.is_alive():
            release_parent.send(True)
        process.join(timeout=10)
    assert process.exitcode == 0
    await _index(comp, source)


# --- the detached lock's name ---------------------------------------------------


def test_spellings_of_one_file_share_a_detached_lock(tmp_path):
    target = tmp_path / "Café" / "Note.md"
    target.parent.mkdir()
    target.write_text(BODY, encoding="utf-8")
    key = detached_source_lock_key(target)

    assert detached_source_lock_key(Path(str(target).upper())) == key
    assert detached_source_lock_key(Path(str(target).lower())) == key
    assert detached_source_lock_key(Path(unicodedata.normalize("NFD", str(target)))) == key
    assert detached_source_lock_key(Path(unicodedata.normalize("NFC", str(target)))) == key
    assert detached_source_lock_key(tmp_path / "Café" / "other.md") != key


def test_an_alias_and_its_target_share_a_detached_lock(tmp_path):
    target = tmp_path / "target.md"
    target.write_text(BODY, encoding="utf-8")
    alias = tmp_path / "alias.md"
    _symlink_or_skip(alias, target)

    assert detached_source_lock_key(alias) == detached_source_lock_key(target)
    assert _atomic._detached_lock_path(alias).parent == runtime_dir() / "source-locks"


def _candidate(source: Path) -> NamespaceChunkCandidate:
    return NamespaceChunkCandidate(
        chunk_id=uuid4(), source_file=source, source_file_text=str(source), namespace="old"
    )


async def test_two_sources_that_share_a_detached_lock_do_not_wait_for_each_other(
    tmp_path, short_budgets
):
    """The lock is not reentrant; the namespace service takes each key once."""
    first, second = tmp_path / "Notes.md", tmp_path / "notes.md"
    first.write_text(BODY, encoding="utf-8")
    assert detached_source_lock_key(first) == detached_source_lock_key(second)
    candidates = [_candidate(first), _candidate(second)]
    storage = AsyncMock()
    storage.transaction = MagicMock()
    storage.list_namespace_chunk_candidates = AsyncMock(return_value=candidates)
    expected = NamespaceRenameResult(2, False, False)
    storage.rename_namespace = AsyncMock(return_value=expected)

    result = await namespace_management.rename_namespace(
        storage, "old", "new", is_managed_source=lambda _source: False
    )

    assert result is expected
    assert not list(tmp_path.glob(".*.lock"))


# --- the runtime directory ----------------------------------------------------


@posix_modes
async def test_a_missing_runtime_directory_is_created_by_its_own_validator(host_source):
    comp, source = host_source
    assert not runtime_dir().exists()

    await _index(comp, source)

    assert validate_runtime_dir(runtime_dir())
    assert stat.S_IMODE(runtime_dir().stat().st_mode) == 0o700
    assert stat.S_IMODE((runtime_dir() / "source-locks").stat().st_mode) == 0o700


@posix_modes
@pytest.mark.parametrize("operation", ("index", "assign"))
async def test_an_unsafe_runtime_directory_fails_the_operation_and_creates_nothing(
    host_source, short_budgets, operation
):
    """No fallback to a sidecar beside the source."""
    comp, source = host_source
    await _index(comp, source)
    os.chmod(runtime_dir(), 0o755)
    try:
        if operation == "index":
            with pytest.raises(RuntimeDirValidationError):
                await comp.index_engine.index_file(source, ingress_guarded=True, force=True)
        else:
            with pytest.raises(NamespaceMutationBusyError):
                await _run(comp, operation, source)
    finally:
        os.chmod(runtime_dir(), 0o700)

    assert _names(source.parent) == ["note.md"]


async def test_the_runtime_directory_removed_during_the_wait_is_made_again_validated(tmp_path):
    """Nothing on this path makes a parent directory; only the validator creates the root."""
    source = tmp_path / "note.md"
    async with async_detached_source_lock(source, timeout=1):
        pass
    lock_path = _atomic._detached_lock_path(source)
    intra = _atomic._intra_async_lock_for(lock_path)
    await intra.acquire()

    async def acquire() -> None:
        async with async_detached_source_lock(source, timeout=5):
            pass

    waiter = asyncio.create_task(acquire())
    await asyncio.sleep(0.05)
    assert not waiter.done()
    for leftover in sorted(runtime_dir().rglob("*"), reverse=True):
        leftover.rmdir() if leftover.is_dir() else leftover.unlink()
    runtime_dir().rmdir()
    intra.release()
    await waiter

    assert validate_runtime_dir(runtime_dir())
    assert lock_path.is_file()


async def test_a_link_in_place_of_the_detached_lock_directory_is_refused(host_source, tmp_path):
    comp, source = host_source
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    async with async_detached_source_lock(tmp_path / "other.md", timeout=1):
        pass
    leaf = runtime_dir() / "source-locks"
    for lock_file in leaf.iterdir():
        lock_file.unlink()
    leaf.rmdir()
    _symlink_or_skip(leaf, elsewhere)

    with pytest.raises(NotADirectoryError):
        await comp.index_engine.index_file(source, ingress_guarded=True)

    assert _names(elsewhere) == []
    assert _names(source.parent) == ["note.md"]


async def test_a_junction_in_place_of_the_detached_lock_directory_is_refused(tmp_path, monkeypatch):
    """A junction keeps a directory's mode bits, so the no-follow ``stat`` alone passes it."""
    source = tmp_path / "note.md"
    async with async_detached_source_lock(source, timeout=1):
        pass
    leaf = runtime_dir() / "source-locks"
    real = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda self: self == leaf or real(self))

    with pytest.raises(NotADirectoryError):
        async with async_detached_source_lock(source, timeout=1):
            pass


# --- the existing-only acquire ------------------------------------------------


async def test_no_sidecar_yields_false_and_creates_none(tmp_path):
    source = tmp_path / "note.md"
    source.write_text(BODY, encoding="utf-8")

    async with async_existing_memory_file_lock(source, timeout=1) as held:
        assert held is False

    assert _names(tmp_path) == ["note.md"]


async def test_a_sidecar_that_is_not_a_regular_file_is_not_a_sidecar(tmp_path):
    source = tmp_path / "note.md"
    source.write_text(BODY, encoding="utf-8")
    memory_lock_path(source).mkdir()

    async with async_existing_memory_file_lock(source, timeout=1) as held:
        assert held is False


async def test_a_sidecar_swapped_for_a_link_after_the_stat_is_not_locked_through(
    tmp_path, monkeypatch
):
    """Where the open cannot refuse a link, the descriptor is checked against the name.

    The other file IS opened in that case, and this pins what happens next: it
    is not locked, and its descriptor is closed.
    """
    source = tmp_path / "note.md"
    source.write_text(BODY, encoding="utf-8")
    sidecar = memory_lock_path(source)
    sidecar.touch()
    other = tmp_path / "somewhere-else.lock"
    other.touch()
    real_open = os.open
    opened: list[int] = []
    asked: list[int] = []

    def open_the_other_file(path, flags, *args, **kwargs):
        # What following a link swapped in after the ``stat`` would return.
        if Path(path) == sidecar:
            asked.append(flags)
            path = other
        fd = real_open(path, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(_atomic.os, "open", open_the_other_file)

    async with async_existing_memory_file_lock(source, timeout=1) as held:
        assert held is False

    (fd,) = opened
    with pytest.raises(OSError):
        os.fstat(fd)
    # Where the platform can refuse a link at the open, it is asked to: the
    # check above is for the platforms that cannot. Never ``O_CREAT``.
    (flags,) = asked
    assert flags & getattr(os, "O_NOFOLLOW", 0) == getattr(os, "O_NOFOLLOW", 0)
    assert not flags & os.O_CREAT


async def test_a_creating_acquirer_in_this_loop_waits_for_the_existing_only_one(tmp_path):
    """Its in-process layer is the one a creating acquirer takes first, for the same spelling."""
    source = tmp_path / "note.md"
    source.write_text(BODY, encoding="utf-8")

    async with async_existing_memory_file_lock(source, timeout=1) as held:
        assert held is False
        with pytest.raises(TimeoutError, match="in-process"):
            async with async_file_lock(memory_lock_path(source), timeout=0.1):
                pass

    assert _names(tmp_path) == ["note.md"]


async def test_an_alias_of_the_source_is_covered_in_process(tmp_path):
    """Two paths that resolve to one path have one lock path, and so one in-process layer."""
    source = tmp_path / "note.md"
    source.write_text(BODY, encoding="utf-8")
    alias = tmp_path / "alias.md"
    _symlink_or_skip(alias, source)
    assert memory_lock_path(alias) == memory_lock_path(source)

    async with async_existing_memory_file_lock(source, timeout=1) as held:
        assert held is False
        with pytest.raises(TimeoutError, match="in-process"):
            async with async_file_lock(memory_lock_path(alias), timeout=0.1):
                pass


async def test_another_case_spelling_is_covered_only_where_it_resolves_alike(tmp_path):
    """The in-process layer keys on the literal lock path; this is the stated limit, pinned.

    Whether another case spelling of an existing file shares that path is the
    resolver's answer, not this module's: Windows resolves it to the spelling
    on disk, so there the two are one lock path and the creating acquirer
    waits. Where the resolver keeps the spelling it was given (macOS, Linux)
    they are two lock paths and nothing in this process stands between them.
    """
    source = tmp_path / "note.md"
    source.write_text(BODY, encoding="utf-8")
    respelled = tmp_path / "NOTE.md"
    resolves_alike = memory_lock_path(respelled) == memory_lock_path(source)

    async with async_existing_memory_file_lock(source, timeout=1) as held:
        assert held is False
        if resolves_alike:
            with pytest.raises(TimeoutError, match="in-process"):
                async with async_file_lock(memory_lock_path(respelled), timeout=0.1):
                    pass
        else:
            async with async_file_lock(memory_lock_path(respelled), timeout=0.1):
                pass
