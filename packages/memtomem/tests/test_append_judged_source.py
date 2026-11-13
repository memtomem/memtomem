"""An append writes only to the file its preflight judged (#3289).

A memory tool reads the file (``read_for_append``), asks the preflight about
those bytes, and then appends. The caller's sidecar binds cooperating writers
only, so another process can replace the path between the preflight and the
append. ``open(path, "a")`` followed a symlink put there: the entry landed in
the link's target, a file nobody judged, past ``confirm_project_shared`` and
the refusal of ``force_unsafe`` in a ``project_shared`` file.

``append_blocks`` now takes the read as ``judged`` and decides at the open:
a file that existed is opened without ``O_CREAT`` and compared by identity on
the descriptor, and a file that did not is created at the judged path by
descriptor, walking from the root without following a link.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from helpers import StubCtx
from memtomem import privacy
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.storage import sqlite_helpers
from memtomem.storage.source_holds import RELINKED_HOLD
from memtomem.storage.sqlite_helpers import norm_path
from memtomem.tools import memory_writer
from test_excluded_target_writes import (
    APPENDERS,
    _append,
    _patch_cli_components,
    _web_client,
    click_unstyle,
)
from memtomem.tools.memory_writer import (
    SourceRemovedError,
    SourceReplacedError,
    append_blocks,
    append_entry,
    read_for_append,
)

_CLEAN_A = "## A\n\nAlphaword entry about deploys\n"
_CLEAN_B = "## B\n\nBravoword entry about reviews\n"
_TEAM = "## Team\n\nTeamword shared conventions\n"
_SECRET = "Gammaword rotation note\npassword: hunter2"
_REPLACED = "was replaced after it was read; nothing was written"

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor walk")


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


def _link_or_skip(link: Path, target: Path) -> None:
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(target)
    except OSError as exc:  # Windows without symlink privilege
        pytest.skip(f"cannot create symlink: {exc}")


def _relink(leaf: Path, target: Path, aside: Path) -> None:
    """Move ``leaf``'s file to ``aside`` and put a link to ``target`` at its path."""
    leaf.rename(aside)
    _link_or_skip(leaf, target)


def _holds(comp) -> dict[str, str]:
    rows = comp.storage._get_db().execute("SELECT source_file, reason FROM held_sources")
    return {row[0]: row[1] for row in rows.fetchall()}


def _swap_before_the_append(monkeypatch, swap) -> list[Path]:
    """Run ``swap`` once, after the preflight, as the append starts: before its open."""
    calls: list[Path] = []
    original = memory_writer.append_blocks

    def swapped(file_path, blocks, *, judged):
        if not calls:
            calls.append(file_path)
            swap()
        return original(file_path, blocks, judged=judged)

    monkeypatch.setattr(memory_writer, "append_blocks", swapped)
    return calls


# --- the writer ---------------------------------------------------------------


def test_an_existing_file_replaced_by_a_link_is_not_followed(tmp_path):
    alpha, bravo = tmp_path / "alpha.md", tmp_path / "bravo.md"
    alpha.write_text(_CLEAN_A, encoding="utf-8")
    bravo.write_text(_CLEAN_B, encoding="utf-8")
    judged = read_for_append(alpha)
    _relink(alpha, bravo, tmp_path / "moved-alpha.md")

    with pytest.raises(SourceReplacedError):
        append_entry(alpha, "gamma", judged=judged)

    assert bravo.read_text(encoding="utf-8") == _CLEAN_B
    assert (tmp_path / "moved-alpha.md").read_text(encoding="utf-8") == _CLEAN_A


def test_a_file_that_was_a_link_all_along_takes_the_append(tmp_path):
    bravo = tmp_path / "bravo.md"
    bravo.write_text(_CLEAN_B, encoding="utf-8")
    alpha = tmp_path / "alpha.md"
    _link_or_skip(alpha, bravo)
    judged = read_for_append(alpha)

    append_entry(alpha, "gamma", judged=judged)

    assert "gamma" in bravo.read_text(encoding="utf-8")
    assert judged.leaf_own is False


def test_an_existing_file_removed_since_the_read_is_not_recreated(tmp_path):
    alpha = tmp_path / "alpha.md"
    alpha.write_text(_CLEAN_A, encoding="utf-8")
    judged = read_for_append(alpha)
    alpha.unlink()

    with pytest.raises(SourceRemovedError):
        append_entry(alpha, "gamma", judged=judged)

    assert not alpha.exists()


def test_an_empty_existing_file_is_appended_to_not_created(tmp_path):
    alpha = tmp_path / "alpha.md"
    alpha.write_text("", encoding="utf-8")
    judged = read_for_append(alpha)

    assert judged.existed and judged.data == b""
    append_entry(alpha, "gamma", judged=judged)
    assert "gamma" in alpha.read_text(encoding="utf-8")


@posix_only
def test_a_fifo_put_in_an_existing_files_place_is_refused_without_blocking(tmp_path):
    alpha = tmp_path / "alpha.md"
    alpha.write_text(_CLEAN_A, encoding="utf-8")
    judged = read_for_append(alpha)
    alpha.unlink()
    os.mkfifo(alpha)
    outcome: list[BaseException | str] = []

    def run() -> None:
        try:
            append_entry(alpha, "gamma", judged=judged)
            outcome.append("written")
        except BaseException as exc:  # noqa: BLE001 — reported to the main thread
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "the open blocked on a FIFO with no reader"
    assert len(outcome) == 1 and isinstance(outcome[0], SourceReplacedError), outcome


def test_without_an_identity_a_relinked_path_is_refused_by_its_resolution(tmp_path):
    """``st_ino == 0`` filesystems: the resolution stands in for the descriptor's identity."""
    alpha, bravo = tmp_path / "alpha.md", tmp_path / "bravo.md"
    alpha.write_text(_CLEAN_A, encoding="utf-8")
    bravo.write_text(_CLEAN_B, encoding="utf-8")
    judged = replace(read_for_append(alpha), identity=None)
    _relink(alpha, bravo, tmp_path / "moved-alpha.md")

    with pytest.raises(SourceReplacedError):
        append_entry(alpha, "gamma", judged=judged)

    assert bravo.read_text(encoding="utf-8") == _CLEAN_B


def test_without_an_identity_an_unchanged_file_takes_the_append(tmp_path):
    alpha = tmp_path / "alpha.md"
    alpha.write_text(_CLEAN_A, encoding="utf-8")
    judged = replace(read_for_append(alpha), identity=None)

    append_entry(alpha, "gamma", judged=judged)

    assert "gamma" in alpha.read_text(encoding="utf-8")


def test_without_an_identity_a_link_taken_away_right_after_the_open_is_refused(
    tmp_path, monkeypatch
):
    """The resolution before the open sees a link the one after it would miss."""
    alpha, bravo = tmp_path / "alpha.md", tmp_path / "bravo.md"
    alpha.write_text(_CLEAN_A, encoding="utf-8")
    bravo.write_text(_CLEAN_B, encoding="utf-8")
    judged = replace(read_for_append(alpha), identity=None)
    aside = tmp_path / "moved-alpha.md"
    _relink(alpha, bravo, aside)
    original = memory_writer._open_existing

    def open_then_put_back(*args, **kwargs):
        handle = original(*args, **kwargs)
        alpha.unlink()
        aside.rename(alpha)
        return handle

    monkeypatch.setattr(memory_writer, "_open_existing", open_then_put_back)

    with pytest.raises(SourceReplacedError):
        append_entry(alpha, "gamma", judged=judged)

    assert bravo.read_text(encoding="utf-8") == _CLEAN_B


def test_without_an_identity_a_link_put_in_place_after_the_first_check_is_refused(
    tmp_path, monkeypatch
):
    """The resolution after the open sees a link the one before it could not."""
    alpha, bravo = tmp_path / "alpha.md", tmp_path / "bravo.md"
    alpha.write_text(_CLEAN_A, encoding="utf-8")
    bravo.write_text(_CLEAN_B, encoding="utf-8")
    judged = replace(read_for_append(alpha), identity=None)
    original = memory_writer._open_existing

    def relink_then_open(*args, **kwargs):
        _relink(alpha, bravo, tmp_path / "moved-alpha.md")
        return original(*args, **kwargs)

    monkeypatch.setattr(memory_writer, "_open_existing", relink_then_open)

    with pytest.raises(SourceReplacedError):
        append_entry(alpha, "gamma", judged=judged)

    assert bravo.read_text(encoding="utf-8") == _CLEAN_B


@pytest.mark.parametrize("make_fails", ["FileIO", "TextIOWrapper"])
def test_a_failed_wrapper_closes_the_descriptor_once(tmp_path, monkeypatch, make_fails):
    """One owner per layer: the failure surfaces, and the descriptor ends closed.

    ``open(fd, ...)`` closes ``fd`` itself when it fails after making its raw
    file, so a second close raised ``EBADF`` over the real error, or closed an
    unrelated descriptor that had reused the number.
    """
    fd = os.open(tmp_path / "new.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)

    def fail(*_args, **_kwargs):
        raise MemoryError("injected")

    monkeypatch.setattr(memory_writer.io, make_fails, fail)

    with pytest.raises(MemoryError):
        memory_writer._text_appender(fd)

    monkeypatch.undo()
    with pytest.raises(OSError):
        os.fstat(fd)


def test_a_new_file_is_created_with_its_missing_directories(tmp_path):
    target = tmp_path / "sub" / "deeper" / "new.md"
    judged = read_for_append(target)

    assert judged.existed is False and judged.leaf_own is True
    append_entry(target, "gamma", judged=judged)

    assert "gamma" in target.read_text(encoding="utf-8")


def test_an_empty_batch_touches_nothing(tmp_path):
    target = tmp_path / "sub" / "new.md"
    judged = read_for_append(target)

    assert append_blocks(target, [], judged=judged) == ""
    assert not (tmp_path / "sub").exists()


@pytest.mark.parametrize("occupant", ["link", "directory", "file"])
def test_a_new_files_name_taken_since_the_read_is_refused(tmp_path, occupant):
    target = tmp_path / "new.md"
    elsewhere = tmp_path / "elsewhere.md"
    judged = read_for_append(target)
    if occupant == "link":
        _link_or_skip(target, elsewhere)
    elif occupant == "directory":
        target.mkdir()
    else:
        target.write_text(_CLEAN_B, encoding="utf-8")

    with pytest.raises(SourceReplacedError):
        append_entry(target, "gamma", judged=judged)

    assert not elsewhere.exists()
    if occupant == "file":
        assert target.read_text(encoding="utf-8") == _CLEAN_B


@posix_only
def test_a_new_files_parent_replaced_by_a_link_is_not_followed(tmp_path):
    memories = tmp_path / "memories"
    (memories / "sub").mkdir(parents=True)
    shared = tmp_path / "shared"
    shared.mkdir()
    target = memories / "sub" / "new.md"
    judged = read_for_append(target)
    _relink(memories / "sub", shared, tmp_path / "moved-sub")

    with pytest.raises(SourceReplacedError):
        append_entry(target, "gamma", judged=judged)

    assert list(shared.iterdir()) == []


@posix_only
def test_a_missing_parent_under_an_ancestor_relinked_since_the_read_is_not_made_there(
    tmp_path,
):
    """No directory is made through a relinked ancestor, either (pathname ``mkdir`` did)."""
    memories = tmp_path / "memories"
    (memories / "sub").mkdir(parents=True)
    shared = tmp_path / "shared"
    shared.mkdir()
    target = memories / "sub" / "missing" / "deeper" / "new.md"
    judged = read_for_append(target)
    _relink(memories / "sub", shared, tmp_path / "moved-sub")

    with pytest.raises(SourceReplacedError):
        append_entry(target, "gamma", judged=judged)

    assert list(shared.iterdir()) == []


@posix_only
def test_an_ancestor_swapped_and_put_back_during_the_walk_is_refused(tmp_path, monkeypatch):
    """The ancestor is a link only while the walk passes it, then is put back."""
    memories = tmp_path / "memories"
    (memories / "sub").mkdir(parents=True)
    shared = tmp_path / "shared"
    shared.mkdir()
    target = memories / "sub" / "missing" / "new.md"
    judged = read_for_append(target)
    original = memory_writer._enter_directory
    aside = tmp_path / "moved-sub"

    def swap_around(parent_fd, name, source_path):
        if name != "sub":
            return original(parent_fd, name, source_path)
        _relink(memories / "sub", shared, aside)
        try:
            return original(parent_fd, name, source_path)
        finally:
            (memories / "sub").unlink()
            aside.rename(memories / "sub")

    monkeypatch.setattr(memory_writer, "_enter_directory", swap_around)

    with pytest.raises(SourceReplacedError):
        append_entry(target, "gamma", judged=judged)

    assert list(shared.iterdir()) == []
    assert not (memories / "sub" / "missing").exists()


def test_without_dir_fd_a_file_gone_before_it_is_looked_at_is_refused(tmp_path, monkeypatch):
    """A typed refusal, so every surface releases what it claimed (``mem_agent_share``)."""
    monkeypatch.setattr(memory_writer, "_HAS_DIR_FD", False)
    target = tmp_path / "new.md"
    judged = read_for_append(target)
    real_lstat = os.lstat

    def gone(path, *args, **kwargs):
        if Path(path) == target:
            raise FileNotFoundError(path)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", gone)

    with pytest.raises(SourceReplacedError):
        append_entry(target, "gamma", judged=judged)


def test_a_dangling_link_read_as_empty_creates_its_judged_target(tmp_path):
    """The read resolves a dangling link to its target, and that is where the file is made."""
    target = tmp_path / "real" / "t.md"
    leaf = tmp_path / "l.md"
    _link_or_skip(leaf, target)
    judged = read_for_append(leaf)

    assert judged.existed is False and judged.source_path == target.resolve()
    append_entry(leaf, "gamma", judged=judged)

    assert "gamma" in target.read_text(encoding="utf-8")
    assert leaf.is_symlink()


@posix_only
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores the search-only mode"
)
def test_a_search_only_ancestor_does_not_stop_the_create(tmp_path):
    """The walk needs search permission only, as creating a file by path did."""
    locked = tmp_path / "locked"
    (locked / "inner").mkdir(parents=True)
    target = locked / "inner" / "new.md"
    judged = read_for_append(target)
    locked.chmod(0o111)
    try:
        append_entry(target, "gamma", judged=judged)
    finally:
        locked.chmod(0o755)

    assert "gamma" in target.read_text(encoding="utf-8")


def test_without_dir_fd_a_create_that_followed_a_link_is_refused_and_left(tmp_path, monkeypatch):
    """Windows' ``O_EXCL`` follows a reparse point; what was claimed is checked instead."""
    monkeypatch.setattr(memory_writer, "_HAS_DIR_FD", False)
    target = tmp_path / "new.md"
    judged = read_for_append(target)
    real_lstat = os.lstat

    def lstat_sees_a_link(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if Path(path) == target:
            fields = list(info)
            fields[0] = stat.S_IFLNK | 0o777
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, "lstat", lstat_sees_a_link)

    with pytest.raises(SourceReplacedError):
        append_entry(target, "gamma", judged=judged)

    monkeypatch.undo()
    # Left where it is: no pathname delete can be sure it removes that file.
    assert target.read_text(encoding="utf-8") == ""


def test_without_dir_fd_a_name_taken_by_a_directory_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_writer, "_HAS_DIR_FD", False)
    target = tmp_path / "new.md"
    judged = read_for_append(target)
    target.mkdir()

    with pytest.raises(SourceReplacedError):
        append_entry(target, "gamma", judged=judged)


def test_without_dir_fd_a_new_file_is_created(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_writer, "_HAS_DIR_FD", False)
    target = tmp_path / "sub" / "new.md"
    judged = read_for_append(target)

    append_entry(target, "gamma", judged=judged)

    assert "gamma" in target.read_text(encoding="utf-8")


def test_leaf_own_compares_case_the_way_the_platform_does(tmp_path, monkeypatch):
    """A leaf typed in another case than it is stored still names its own key (Windows)."""
    monkeypatch.setattr(sqlite_helpers.os.path, "normcase", str.lower)
    own = sqlite_helpers.own_leaf_key(tmp_path / "NOTE.md")

    assert sqlite_helpers.leaf_names_own_key(own, tmp_path.resolve() / "note.md")


# --- mem_add / mem_batch_add ---------------------------------------------------


def _register(comp, root: Path) -> Path:
    pdir = root / ".memtomem" / "memories"
    pdir.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [pdir]
    return pdir


def _alpha_and_bravo(mem_dir: Path) -> tuple[Path, Path]:
    alpha = mem_dir / "alpha.md"
    alpha.write_text(_CLEAN_A, encoding="utf-8")
    bravo = mem_dir / "bravo.md"
    bravo.write_text(_CLEAN_B, encoding="utf-8")
    return alpha, bravo


def _ctx(comp) -> StubCtx:
    return StubCtx(AppContext.from_components(comp))


async def test_mem_add_does_not_follow_a_link_put_in_place_after_the_preflight(
    bm25_only_components, tmp_path, monkeypatch
):
    comp, mem_dir = bm25_only_components
    alpha, bravo = _alpha_and_bravo(mem_dir)
    stats = await comp.index_engine.index_file(alpha, ingress_guarded=True)
    assert stats.indexed_chunks == 1, stats
    read_key = norm_path(alpha)  # before the swap: resolved after, it is bravo's
    calls = _swap_before_the_append(
        monkeypatch, lambda: _relink(alpha, bravo, tmp_path / "moved-alpha.md")
    )

    result = await memory_crud.mem_add(content="gamma entry", file=str(alpha), ctx=_ctx(comp))  # type: ignore[arg-type]

    assert calls, "the append never ran, so nothing was swapped"
    assert _REPLACED in result, result
    assert bravo.read_text(encoding="utf-8") == _CLEAN_B
    assert (tmp_path / "moved-alpha.md").read_text(encoding="utf-8") == _CLEAN_A
    assert _holds(comp) == {read_key: RELINKED_HOLD}


@pytest.mark.parametrize("forced", [False, True], ids=["clean", "forced"])
async def test_mem_add_does_not_reach_a_project_shared_file_through_a_late_link(
    bm25_only_components, tmp_path, monkeypatch, forced
):
    """The two refusals that keep a forced entry out of a git-tracked file are not bypassed."""
    comp, mem_dir = bm25_only_components
    alpha, _ = _alpha_and_bravo(mem_dir)
    team = _register(comp, tmp_path / "proj") / "team.md"
    team.write_text(_TEAM, encoding="utf-8")
    calls = _swap_before_the_append(
        monkeypatch, lambda: _relink(alpha, team, tmp_path / "moved-alpha.md")
    )

    result = await memory_crud.mem_add(  # type: ignore[arg-type]
        content=_SECRET if forced else "gamma entry",
        file=str(alpha),
        force_unsafe=forced,
        ctx=_ctx(comp),
    )

    assert calls, "the append never ran, so nothing was swapped"
    assert _REPLACED in result, result
    assert team.read_text(encoding="utf-8") == _TEAM


async def test_a_refused_append_releases_its_idempotency_claim(
    bm25_only_components, tmp_path, monkeypatch
):
    comp, mem_dir = bm25_only_components
    alpha, bravo = _alpha_and_bravo(mem_dir)
    _swap_before_the_append(monkeypatch, lambda: _relink(alpha, bravo, tmp_path / "moved.md"))
    ctx = _ctx(comp)

    refused = await memory_crud.mem_add(  # type: ignore[arg-type]
        content="gamma entry", file=str(alpha), idempotency_key="k-3289", ctx=ctx
    )
    assert _REPLACED in refused, refused
    alpha.unlink()
    (tmp_path / "moved.md").rename(alpha)

    retried = await memory_crud.mem_add(  # type: ignore[arg-type]
        content="gamma entry", file=str(alpha), idempotency_key="k-3289", ctx=ctx
    )

    assert retried.startswith("Memory added"), retried
    assert "gamma entry" in alpha.read_text(encoding="utf-8")


async def test_mem_batch_add_does_not_follow_a_link_put_in_place_after_the_preflight(
    bm25_only_components, tmp_path, monkeypatch
):
    comp, mem_dir = bm25_only_components
    alpha, bravo = _alpha_and_bravo(mem_dir)
    calls = _swap_before_the_append(
        monkeypatch, lambda: _relink(alpha, bravo, tmp_path / "moved-alpha.md")
    )

    result = await memory_crud.mem_batch_add(  # type: ignore[arg-type]
        entries=[{"key": "g", "value": "gamma entry"}], file=str(alpha), ctx=_ctx(comp)
    )

    assert calls, "the append never ran, so nothing was swapped"
    assert _REPLACED in result, result
    assert bravo.read_text(encoding="utf-8") == _CLEAN_B


async def test_a_link_put_in_place_after_the_append_is_held_not_indexed(
    bm25_only_components, tmp_path, monkeypatch
):
    """The re-index holds a leaf that was regular when read and is a link now."""
    comp, mem_dir = bm25_only_components
    alpha, bravo = _alpha_and_bravo(mem_dir)
    stats = await comp.index_engine.index_file(alpha, ingress_guarded=True)
    assert stats.indexed_chunks == 1, stats
    read_key = norm_path(alpha)  # before the swap: resolved after, it is bravo's
    engine = comp.index_engine
    original = engine.index_file

    async def swap_then_index(path, *args, **kwargs):
        if path == alpha and not alpha.is_symlink():
            _relink(alpha, bravo, tmp_path / "moved-alpha.md")
        return await original(path, *args, **kwargs)

    monkeypatch.setattr(engine, "index_file", swap_then_index)

    result = await memory_crud.mem_add(content="gamma entry", file=str(alpha), ctx=_ctx(comp))  # type: ignore[arg-type]

    assert "replaced by a symbolic link before it was re-indexed" in result, result
    assert "gamma entry" in (tmp_path / "moved-alpha.md").read_text(encoding="utf-8")
    assert _holds(comp) == {read_key: RELINKED_HOLD}
    # Followed, the re-index would have indexed the link's target as this write.
    assert await comp.storage.list_chunks_by_source(bravo) == []


# --- every append surface -------------------------------------------------------
#
# Driven the way ``test_excluded_target_writes`` drives them. Every surface reaches
# ``memory_writer.append_blocks`` (the web add through ``system.append_entry``, which
# calls it), so swapping inside it is a swap after each surface's preflight.

_RELINKED_AFTER = "replaced by a symbolic link before it was re-indexed"


@pytest.fixture
async def day_file(bm25_only_components):
    """Today's day file, indexed: the file every appender writes by default."""
    from datetime import datetime, timezone

    comp, mem_dir = bm25_only_components
    source = mem_dir / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    source.write_text(_CLEAN_A, encoding="utf-8")
    assert (await comp.index_engine.index_file(source, ingress_guarded=True)).indexed_chunks == 1
    return comp, mem_dir, source


def _swap_whatever_is_appended_to(monkeypatch, decoy: Path, aside: Path) -> list[Path]:
    """Before the open, put a link to ``decoy`` where the append is about to write."""

    def swap(path: Path) -> None:
        if path.exists():
            path.rename(aside)
        _link_or_skip(path, decoy)

    calls: list[Path] = []
    original = memory_writer.append_blocks

    def swapped(file_path, blocks, *, judged):
        if not calls:
            calls.append(file_path)
            swap(file_path)
        return original(file_path, blocks, judged=judged)

    monkeypatch.setattr(memory_writer, "append_blocks", swapped)
    return calls


def _refusal_text(surface: str, result, capsys) -> str:
    """The refusal a surface reported, failing if it reported success."""
    if surface in ("mcp_add", "mcp_batch_add"):
        assert result.startswith("Error"), result
        return result
    if surface in ("cli_add", "cli_agent_share"):
        assert result is not None, "the CLI reported success"
        return result
    if surface == "shell_add":
        return click_unstyle(result)
    if surface == "web_add":
        assert result.status_code == 409, result.text
        body = result.json()["detail"]
        assert body["detail"] == "index_blocked" and body["written"] is False, body
        return body["message"]
    if surface == "langgraph_add":
        assert result["error"] == "index_blocked" and result["written"] is False, result
        return result["detail"]
    raise AssertionError(surface)


@pytest.mark.parametrize("surface", APPENDERS)
async def test_no_appender_follows_a_link_put_in_place_after_its_preflight(
    day_file, tmp_path, monkeypatch, capsys, surface
):
    comp, mem_dir, source = day_file
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    decoy = tmp_path / "decoy.md"
    decoy.write_text(_CLEAN_B, encoding="utf-8")
    calls = _swap_whatever_is_appended_to(monkeypatch, decoy, tmp_path / "aside.md")

    result = await _append(surface, comp, monkeypatch, capsys, str(chunk.id))

    assert calls, "the append never ran, so nothing was swapped"
    assert _REPLACED in _refusal_text(surface, result, capsys)
    assert decoy.read_text(encoding="utf-8") == _CLEAN_B
    if (tmp_path / "aside.md").exists():
        assert "NEW ENTRY" not in (tmp_path / "aside.md").read_text(encoding="utf-8")
    pending = comp.storage._get_db().execute("SELECT tool, key FROM idempotency_ledger").fetchall()
    assert pending == [], "a refused append must leave its key re-runnable"


@pytest.mark.parametrize("surface", APPENDERS)
async def test_every_appender_reports_a_link_put_in_place_after_its_append(
    day_file, tmp_path, monkeypatch, capsys, surface
):
    """The re-index holds the file instead of indexing the link's target, and says so."""
    comp, mem_dir, source = day_file
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    decoy = tmp_path / "decoy.md"
    decoy.write_text(_CLEAN_B, encoding="utf-8")
    engine = comp.index_engine
    original = engine.index_file
    swapped: list[Path] = []
    had_rows: list[bool] = []

    async def swap_then_index(path, *args, **kwargs):
        if kwargs.get("lock_held") and not swapped:
            swapped.append(path)
            had_rows.append(bool(await comp.storage.list_chunks_by_source(path)))
            path.rename(tmp_path / "aside.md")
            _link_or_skip(path, decoy)
        return await original(path, *args, **kwargs)

    monkeypatch.setattr(engine, "index_file", swap_then_index)
    capsys.readouterr()

    result = await _append(surface, comp, monkeypatch, capsys, str(chunk.id))

    assert swapped, "the re-index never ran"
    if surface in ("mcp_add", "mcp_batch_add"):
        warning = result
    elif surface in ("cli_add", "cli_agent_share"):
        assert result is None, result
        warning = capsys.readouterr().err
    elif surface == "shell_add":
        warning = click_unstyle(result)
    elif surface == "web_add":
        assert result.status_code == 200, result.text
        warning = result.json()["warning"]
    else:
        warning = result["warning"]
    assert _RELINKED_AFTER in warning, warning
    # Followed, the re-index would have indexed the link's target as this write.
    assert await comp.storage.list_chunks_by_source(decoy) == []
    # The written file's own rows are held, when it had any (``mm agent share``
    # writes another namespace's day file, which may be new).
    held = _holds(comp).get(sqlite_helpers.own_leaf_key(swapped[0]))
    assert (held == RELINKED_HOLD) is had_rows[0], (held, had_rows)


async def test_review_approve_does_not_follow_a_late_link(day_file, tmp_path, monkeypatch):
    import click

    from memtomem.cli.review_cmd import _decide
    from memtomem.formation import propose_memory_candidate

    comp, _, source = day_file
    _patch_cli_components(monkeypatch, comp)
    candidate, _created = await propose_memory_candidate(
        comp.storage,
        "Decision: a durable fact",
        source="test",
        source_ref="ref-1",
        idempotency_key="proposal-1",
    )
    decoy = tmp_path / "decoy.md"
    decoy.write_text(_CLEAN_B, encoding="utf-8")
    _swap_whatever_is_appended_to(monkeypatch, decoy, tmp_path / "aside.md")

    with pytest.raises(click.ClickException) as raised:
        await _decide(candidate["id"], "approved", "alice", "")

    assert _REPLACED in raised.value.message
    assert decoy.read_text(encoding="utf-8") == _CLEAN_B
    # Released, not approved: nothing was written, so it can be approved again.
    assert (await comp.storage.get_memory_candidate(candidate["id"]))["status"] == "pending"


async def test_scratch_promote_does_not_follow_a_late_link(day_file, tmp_path, monkeypatch):
    comp, _, _source = day_file
    await comp.storage.scratch_set("note", "promote me")
    decoy = tmp_path / "decoy.md"
    decoy.write_text(_CLEAN_B, encoding="utf-8")
    _swap_whatever_is_appended_to(monkeypatch, decoy, tmp_path / "aside.md")

    async with _web_client(comp) as client:
        resp = await client.post("/api/scratch/note/promote", json={})

    assert resp.status_code == 409, resp.text
    assert _REPLACED in resp.json()["detail"]["message"]
    assert decoy.read_text(encoding="utf-8") == _CLEAN_B
    assert not (await comp.storage.scratch_get("note"))["promoted"]


async def test_web_add_merges_tags_into_the_new_entry_only(day_file):
    """``pre_lines`` is counted from the judged bytes now (#3289); the merge still lands."""
    comp, _, source = day_file
    async with _web_client(comp) as client:
        resp = await client.post(
            "/api/add", json={"content": "## Own heading\n\nNEW ENTRY", "tags": ["fresh"]}
        )

    assert resp.status_code == 200, resp.text
    chunks = await comp.storage.list_chunks_by_source(source)
    tagged = [c for c in chunks if "fresh" in c.metadata.tags]
    assert tagged and all("NEW ENTRY" in c.content for c in tagged), chunks


def _relink_inside_the_reindex(monkeypatch, comp, decoy: Path, aside: Path) -> None:
    """Replace the appended file with a link to ``decoy`` as its re-index starts."""
    engine = comp.index_engine
    original = engine.index_file
    done: list[Path] = []

    async def swap_then_index(path, *args, **kwargs):
        if kwargs.get("lock_held") and not done:
            done.append(path)
            path.rename(aside)
            _link_or_skip(path, decoy)
        return await original(path, *args, **kwargs)

    monkeypatch.setattr(engine, "index_file", swap_then_index)


@pytest.mark.parametrize("tool", ["mem_add", "mem_batch_add"])
async def test_a_relinked_append_marks_the_session_record_incomplete(
    day_file, tmp_path, monkeypatch, tool
):
    """No new ids reach provenance, so the session must not claim a full record."""
    from memtomem.server.tools.session import mem_session_end, mem_session_start

    comp, _, _ = day_file
    decoy = tmp_path / "decoy.md"
    decoy.write_text(_CLEAN_B, encoding="utf-8")
    _relink_inside_the_reindex(monkeypatch, comp, decoy, tmp_path / "aside.md")
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    await mem_session_start(ctx=ctx)
    session_id = app.current_session_id

    if tool == "mem_add":
        result = await memory_crud.mem_add("NEW ENTRY", ctx=ctx)
    else:
        result = await memory_crud.mem_batch_add([{"content": "NEW ENTRY"}], ctx=ctx)

    assert _RELINKED_AFTER in result, result
    row = await app.storage.get_session(session_id)
    assert row["metadata"].get("provenance_incomplete") is True
    await mem_session_end(ctx=ctx)


async def test_reflect_save_links_nothing_when_relinked_during_the_append(
    bm25_only_components, tmp_path, monkeypatch
):
    """Nothing was indexed, so the link step is skipped, not reported as unattributable."""
    from memtomem.server.tools import reflection

    comp, mem_dir = bm25_only_components
    source = mem_dir / "reflections.md"
    source.write_text(_CLEAN_A, encoding="utf-8")
    assert (await comp.index_engine.index_file(source, ingress_guarded=True)).indexed_chunks == 1
    related = mem_dir / "related.md"
    related.write_text("## Related\n\nA chunk the insight draws on.\n", encoding="utf-8")
    assert (await comp.index_engine.index_file(related, ingress_guarded=True)).indexed_chunks == 1
    (related_chunk,) = await comp.storage.list_chunks_by_source(related)
    db = comp.storage._get_db()
    relations_before = db.execute("SELECT * FROM chunk_relations").fetchall()
    decoy = tmp_path / "decoy.md"
    decoy.write_text(_CLEAN_B, encoding="utf-8")
    _relink_inside_the_reindex(monkeypatch, comp, decoy, tmp_path / "aside.md")

    result = await reflection.mem_reflect_save(
        "A new insight", related_chunks=[str(related_chunk.id)], ctx=_ctx(comp)
    )

    assert result.startswith("Insight saved."), result
    assert _RELINKED_AFTER in result, result
    assert reflection._UNLINKED_NOTICE not in result, result
    assert db.execute("SELECT * FROM chunk_relations").fetchall() == relations_before


async def test_review_approve_reports_a_link_put_in_place_after_its_append(
    day_file, tmp_path, monkeypatch, capsys
):
    import json

    from memtomem.cli.review_cmd import _decide
    from memtomem.formation import propose_memory_candidate

    comp, _, source = day_file
    read_key = norm_path(source)
    _patch_cli_components(monkeypatch, comp)
    candidate, _created = await propose_memory_candidate(
        comp.storage,
        "Decision: a durable fact",
        source="test",
        source_ref="ref-1",
        idempotency_key="proposal-1",
    )
    decoy = tmp_path / "decoy.md"
    decoy.write_text(_CLEAN_B, encoding="utf-8")
    _relink_inside_the_reindex(monkeypatch, comp, decoy, tmp_path / "aside.md")
    capsys.readouterr()

    await _decide(candidate["id"], "approved", "alice", "")

    assert _RELINKED_AFTER in json.loads(capsys.readouterr().out)["warning"]
    assert await comp.storage.list_chunks_by_source(decoy) == []
    assert _holds(comp) == {read_key: RELINKED_HOLD}


async def test_scratch_promote_reports_a_link_put_in_place_after_its_append(
    day_file, tmp_path, monkeypatch
):
    comp, _, source = day_file
    read_key = norm_path(source)
    await comp.storage.scratch_set("note", "promote me")
    decoy = tmp_path / "decoy.md"
    decoy.write_text(_CLEAN_B, encoding="utf-8")
    _relink_inside_the_reindex(monkeypatch, comp, decoy, tmp_path / "aside.md")

    async with _web_client(comp) as client:
        resp = await client.post("/api/scratch/note/promote", json={})

    assert resp.status_code == 200, resp.text
    assert _RELINKED_AFTER in resp.json()["warning"]
    assert await comp.storage.list_chunks_by_source(decoy) == []
    assert _holds(comp) == {read_key: RELINKED_HOLD}


def test_the_relinked_warning_names_the_key_the_hold_used(tmp_path, monkeypatch):
    """The hold is placed under the folded key, so the warning is judged by it too.

    ``relinked_after_write_notice`` says rows could not be hidden when the key
    it is given differs from its folded form; an unfolded (NFD) spelling of the
    read path would say so though the hold covered them.
    """
    import unicodedata
    from types import SimpleNamespace

    from memtomem.tools.memory_mutation import append_warning

    monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", True)
    nfd = Path(unicodedata.normalize("NFD", str(tmp_path / "café.md")))
    judged = replace(read_for_append(tmp_path / "x.md"), source_path=nfd)

    warning = append_warning(SimpleNamespace(excluded_files=0, relinked_files=1), judged)

    assert warning is not None and "chunks are held" in warning, warning
