"""A git worktree nested under an indexed root is a second copy (#2474).

The fixtures run real ``git`` rather than hand-writing ``.git`` files, because the
rule is about what git actually puts on disk. What it writes, measured with git
2.54, is::

    plain checkout        gitdir: <repo>/.git/worktrees/<name>
    worktree of worktree  gitdir: <repo>/.git/worktrees/<name>
    bare repository       gitdir: <repo>.git/worktrees/<name>
    --relative-paths      gitdir: ../../.git/worktrees/<name>
    submodule             gitdir: ../../.git/modules/<path>

so no directory-name test separates a worktree from a submodule across all of them.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from memtomem.indexing.engine import (
    _GIT_MARKER_LIMIT,
    _build_exclude_spec,
    _count_files_on_disk,
    _gitdir_target,
    _is_linked_worktree,
    _path_is_excluded,
    _read_git_marker,
    _under_nested_worktree,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs a git binary")


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=60
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout with one committed file."""
    root = tmp_path / "repo"
    root.mkdir()
    run("init", "-q", ".", cwd=root)
    run("config", "user.email", "t@example.invalid", cwd=root)
    run("config", "user.name", "t", cwd=root)
    (root / "a.md").write_text("# main\n", encoding="utf-8")
    run("add", "a.md", cwd=root)
    run("commit", "-qm", "init", cwd=root)
    return root


@pytest.fixture
def spec():
    return _build_exclude_spec([])


def test_nested_worktree_file_is_excluded(repo: Path, spec) -> None:
    run("worktree", "add", "-q", str(repo / ".worktrees/wt"), "-b", "wt", cwd=repo)

    assert _path_is_excluded(repo / ".worktrees/wt/a.md", [repo], spec) is True
    assert _path_is_excluded(repo / "a.md", [repo], spec) is False


def test_worktree_written_with_relative_paths_is_excluded(repo: Path, spec) -> None:
    """``--relative-paths`` writes ``gitdir: ../../.git/worktrees/<name>``."""
    try:
        run(
            "worktree",
            "add",
            "--relative-paths",
            "-q",
            str(repo / ".worktrees/rel"),
            "-b",
            "rel",
            cwd=repo,
        )
    except subprocess.CalledProcessError as exc:
        # Skip only for the option this test is about. Treating every git
        # failure as "unsupported" turns a broken fixture into a silent pass.
        if "--relative-paths" not in (exc.stderr or ""):
            raise
        pytest.skip("this git does not support worktree add --relative-paths")

    marker = (repo / ".worktrees/rel/.git").read_text(encoding="utf-8")
    assert not Path(marker.split(":", 1)[1].strip()).is_absolute()
    assert _path_is_excluded(repo / ".worktrees/rel/a.md", [repo], spec) is True


def test_worktree_of_a_bare_repository_is_excluded(tmp_path: Path, repo: Path, spec) -> None:
    """The administration directory is ``<repo>.git/worktrees/<name>`` — no ``.git`` segment."""
    bare = tmp_path / "bare.git"
    run("init", "-q", "--bare", str(bare), cwd=tmp_path)
    run("push", "-q", str(bare), "HEAD:refs/heads/main", cwd=repo)
    root = tmp_path / "root"
    root.mkdir()
    run("worktree", "add", "-q", str(root / "checkout"), "main", cwd=bare)

    marker = (root / "checkout/.git").read_text(encoding="utf-8")
    assert "/.git/worktrees/" not in marker.replace("\\", "/")
    assert _path_is_excluded(root / "checkout/a.md", [root], spec) is True


def test_a_submodule_is_still_indexed(tmp_path: Path, repo: Path, spec) -> None:
    """Submodule administration directories carry neither ``commondir`` nor a backlink."""
    other = tmp_path / "other"
    other.mkdir()
    run("init", "-q", ".", cwd=other)
    run("config", "user.email", "t@example.invalid", cwd=other)
    run("config", "user.name", "t", cwd=other)
    (other / "s.md").write_text("# sub\n", encoding="utf-8")
    run("add", "s.md", cwd=other)
    run("commit", "-qm", "init", cwd=other)
    run(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(other),
        "vendor/sub",
        cwd=repo,
    )

    assert (repo / "vendor/sub/.git").is_file()
    assert _is_linked_worktree(repo / "vendor/sub") is False
    assert _path_is_excluded(repo / "vendor/sub/s.md", [repo], spec) is False


def test_an_ordinary_nested_checkout_is_still_indexed(repo: Path, tmp_path: Path, spec) -> None:
    """A nested clone has a ``.git`` *directory*, which is today's behaviour."""
    nested = repo / "vendor/clone"
    nested.parent.mkdir(parents=True)
    run("clone", "-q", str(repo), str(nested), cwd=tmp_path)

    assert (nested / ".git").is_dir()
    assert _path_is_excluded(nested / "a.md", [repo], spec) is False


def test_a_directory_merely_named_worktrees_is_still_indexed(repo: Path, spec) -> None:
    plain = repo / ".git-worktrees/worktrees/notes"
    plain.mkdir(parents=True)
    (plain / "a.md").write_text("# notes\n", encoding="utf-8")

    assert _path_is_excluded(plain / "a.md", [repo], spec) is False


def test_a_registered_worktree_root_is_indexed(repo: Path, spec) -> None:
    """The overridable half: the owning root itself is never tested."""
    wt = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(wt), "-b", "wt", cwd=repo)

    assert _path_is_excluded(wt / "a.md", [repo], spec) is True
    assert _path_is_excluded(wt / "a.md", [repo, wt], spec) is False


def test_a_file_with_no_owning_root_is_bounded_by_its_repository(repo: Path, spec) -> None:
    """#2486: an unowned file used to be left unchecked, so ``mm index <repo>`` stored the copy."""
    run("worktree", "add", "-q", str(repo / ".worktrees/wt"), "-b", "wt", cwd=repo)

    assert _under_nested_worktree(repo / ".worktrees/wt/a.md", [], None) is True
    assert _under_nested_worktree(repo / "a.md", [], None) is False
    assert _path_is_excluded(repo / ".worktrees/wt/a.md", [], spec) is True


def test_an_unowned_worktree_outside_any_checkout_is_indexed(
    repo: Path, tmp_path: Path, spec
) -> None:
    """With no enclosing checkout there is no bound, so nothing is excluded."""
    top = tmp_path / "top-level-wt"
    run("worktree", "add", "-q", str(top), "-b", "top", cwd=repo)

    assert _is_linked_worktree(top) is True
    assert _under_nested_worktree(top / "a.md", [], None) is False


def test_a_file_directly_in_the_root_has_no_intervening_directory(repo: Path) -> None:
    assert _under_nested_worktree(repo / "a.md", [repo], None) is False


def test_a_malformed_git_file_does_not_exclude_and_does_not_raise(repo: Path, spec) -> None:
    broken = repo / "vendor/odd"
    broken.mkdir(parents=True)
    (broken / ".git").write_text("not a gitdir line\n", encoding="utf-8")
    (broken / "a.md").write_text("# odd\n", encoding="utf-8")

    assert _is_linked_worktree(broken) is False
    assert _path_is_excluded(broken / "a.md", [repo], spec) is False


def test_a_gitdir_pointing_nowhere_does_not_exclude(repo: Path, spec) -> None:
    broken = repo / "vendor/dangling"
    broken.mkdir(parents=True)
    (broken / ".git").write_text("gitdir: /nonexistent/admin/dir\n", encoding="utf-8")
    (broken / "a.md").write_text("# dangling\n", encoding="utf-8")

    assert _path_is_excluded(broken / "a.md", [repo], spec) is False


def test_a_backlink_pointing_elsewhere_does_not_exclude(repo: Path, tmp_path: Path, spec) -> None:
    """A stolen ``commondir`` must not let one directory suppress another."""
    run("worktree", "add", "-q", str(repo / ".worktrees/wt"), "-b", "wt", cwd=repo)
    impostor = repo / "vendor/impostor"
    impostor.mkdir(parents=True)
    admin = repo / ".git/worktrees/wt"
    (impostor / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    (impostor / "a.md").write_text("# impostor\n", encoding="utf-8")

    # The admin dir's backlink names the real worktree, not this directory.
    assert _is_linked_worktree(impostor) is False
    assert _path_is_excluded(impostor / "a.md", [repo], spec) is False


def test_an_embedded_nul_in_the_target_does_not_raise(repo: Path, spec) -> None:
    """``open`` rejects a NUL before the filesystem sees it, with ``ValueError``.

    ``_path_is_excluded`` must fail open rather than propagate: ``budget_audit``
    evaluates exclusions outside its per-source handler, so a raise there aborts
    the whole audit.
    """
    # Not ``nul``: that is a reserved device name on Windows, where
    # ``vendor/nul/.git`` cannot be created at all (CI, #2484).
    odd = repo / "vendor/embedded-nul"
    odd.mkdir(parents=True)
    (odd / ".git").write_text("gitdir: /tmp/a\x00b\n", encoding="utf-8")
    (odd / "a.md").write_text("# odd\n", encoding="utf-8")

    assert _is_linked_worktree(odd) is False
    assert _path_is_excluded(odd / "a.md", [repo], spec) is False


def test_gitdir_target_answers_rather_than_raises_for_an_unstattable_path() -> None:
    """``os.open`` rejects an embedded NUL with ``ValueError`` before the kernel."""
    assert _gitdir_target(Path("/tmp/a\x00b")) is None
    assert _is_linked_worktree(Path("/tmp/a\x00b")) is False


# One expression, not two decorators: a ``skipif`` argument is evaluated at
# import, so ``os.geteuid()`` on its own line breaks *collection* on Windows —
# where the name does not exist — and takes the whole module with it. The
# ``or`` short-circuits before the attribute is touched.
@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="POSIX directory permissions, and root ignores the mode bits",
)
def test_an_unreadable_parent_does_not_raise(repo: Path, spec) -> None:
    """A directory the process cannot enter must not raise out of the gate.

    On Python 3.12 ``Path.is_file`` ignores only ENOENT, ENOTDIR, EBADF and
    ELOOP, so an earlier two-probe revision let ``PermissionError`` escape
    ``_path_is_excluded`` — which ``budget_audit`` evaluates outside its
    per-source handler. Newer interpreters answer differently (3.14 on the
    macOS CI runner did not raise), so this asserts the helper's contract only,
    not the interpreter behaviour that made the bug reachable.
    """
    locked = repo / "vendor/locked"
    inner = locked / "inner"
    inner.mkdir(parents=True)
    (inner / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    (inner / "a.md").write_text("# inner\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        assert _gitdir_target(inner) is None
        assert _path_is_excluded(inner / "a.md", [repo], spec) is False
    finally:
        locked.chmod(0o700)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX fifos")
def test_a_fifo_in_place_of_the_metadata_does_not_block(repo: Path, spec) -> None:
    """A reader on a writerless FIFO blocks forever; ``O_NONBLOCK`` + ``S_ISREG`` does not.

    The whole test would hang rather than fail without the fix, so the timeout
    is the assertion: pytest's own run would never finish.
    """
    marked = repo / "vendor/fifo"
    marked.mkdir(parents=True)
    os.mkfifo(marked / ".git")
    (marked / "a.md").write_text("# fifo\n", encoding="utf-8")

    assert stat.S_ISFIFO(os.stat(marked / ".git").st_mode)
    assert _read_git_marker(marked / ".git") is None
    assert _path_is_excluded(marked / "a.md", [repo], spec) is False


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX fifos")
def test_a_fifo_backlink_does_not_block(repo: Path, spec) -> None:
    """The same hazard one level down, where the admin directory is real."""
    admin = repo / "vendor/admin"
    admin.mkdir(parents=True)
    os.mkfifo(admin / "gitdir")
    marked = repo / "vendor/marked-fifo"
    marked.mkdir(parents=True)
    (marked / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    (marked / "a.md").write_text("# marked\n", encoding="utf-8")

    assert _is_linked_worktree(marked) is False
    assert _path_is_excluded(marked / "a.md", [repo], spec) is False


def test_a_directory_named_git_is_not_read_as_a_marker(repo: Path) -> None:
    """The ``S_ISREG`` check is what replaced the separate ``is_file`` probe."""
    ordinary = repo / "vendor/ordinary"
    (ordinary / ".git").mkdir(parents=True)

    assert _read_git_marker(ordinary / ".git") is None
    assert _gitdir_target(ordinary) is None


@pytest.mark.requires_symlinks
def test_a_symlink_loop_behind_the_backlink_does_not_raise(repo: Path, spec) -> None:
    """A symlink loop behind the backlink must not raise out of the gate.

    ``Path.resolve`` raises ``RuntimeError`` on a loop under Python 3.12 and
    returns a path under 3.13 and 3.14 (measured locally and on the macOS CI
    runner), so the guard only fires on 3.12. The test asserts the helper's
    answer, which is ``False`` either way, and not which interpreter path got
    it there.

    The loop is placed in the backlink's *contents*, not in the ``gitdir:``
    target: a loop in the target makes the read itself fail with ``OSError``
    and never reaches the comparison.
    """
    loop = repo / "vendor/loop"
    loop.mkdir(parents=True)
    first, second = loop / "a", loop / "b"
    first.symlink_to(second)
    second.symlink_to(first)
    admin = repo / "vendor/admin"
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text(f"{first}\n", encoding="utf-8")
    marked = repo / "vendor/marked"
    marked.mkdir(parents=True)
    (marked / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    (marked / "a.md").write_text("# marked\n", encoding="utf-8")

    assert _is_linked_worktree(marked) is False
    assert _path_is_excluded(marked / "a.md", [repo], spec) is False


def test_an_oversized_git_file_is_rejected_unread(repo: Path, spec) -> None:
    """Bounded at the read: a plausible name must not cost its own size in memory."""
    big = repo / "vendor/big"
    big.mkdir(parents=True)
    (big / ".git").write_bytes(b"x" * (_GIT_MARKER_LIMIT + 1))
    (big / "a.md").write_text("# big\n", encoding="utf-8")

    assert _read_git_marker(big / ".git") is None
    assert _path_is_excluded(big / "a.md", [repo], spec) is False


def test_a_marker_at_the_limit_is_still_read(repo: Path) -> None:
    exact = repo / "vendor/exact"
    exact.mkdir(parents=True)
    body = "gitdir: /nowhere\n"
    (exact / ".git").write_bytes(body.encode() + b"#" * (_GIT_MARKER_LIMIT - len(body)))

    assert _read_git_marker(exact / ".git") is not None


@pytest.mark.skipif(os.name == "nt", reason="a backslash is a separator on Windows")
def test_a_backslash_in_a_posix_path_is_not_a_separator(tmp_path: Path, spec) -> None:
    """Rewriting ``\\`` to ``/`` made this worktree undetectable (measured)."""
    root = tmp_path / "back\\slash"
    root.mkdir()
    run("init", "-q", ".", cwd=root)
    run("config", "user.email", "t@example.invalid", cwd=root)
    run("config", "user.name", "t", cwd=root)
    (root / "a.md").write_text("# main\n", encoding="utf-8")
    run("add", "a.md", cwd=root)
    run("commit", "-qm", "init", cwd=root)
    run("worktree", "add", "-q", str(root / "wt"), "-b", "wt", cwd=root)

    assert "\\" in (root / "wt/.git").read_text(encoding="utf-8")
    assert _is_linked_worktree(root / "wt") is True
    assert _path_is_excluded(root / "wt/a.md", [root], spec) is True


def test_the_walk_and_the_disk_count_agree(repo: Path) -> None:
    """The web Sources status counts the same files the walk would index."""
    run("worktree", "add", "-q", str(repo / ".worktrees/wt"), "-b", "wt", cwd=repo)

    on_disk = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*.md"))
    assert ".worktrees/wt/a.md" in on_disk
    assert _count_files_on_disk(repo, frozenset({".md"})) == 1


def test_the_wizard_seed_threshold_skips_the_worktree(repo: Path) -> None:
    """``mm init``'s seed-or-skip gate counts the same files the walk would index."""
    from memtomem.cli._index_progress import _collect_seed_scale

    run("worktree", "add", "-q", str(repo / ".worktrees/wt"), "-b", "wt", cwd=repo)

    assert len(list(repo.rglob("*.md"))) == 2
    count, total = _collect_seed_scale(repo)
    assert count == 1
    assert total == (repo / "a.md").stat().st_size


def test_the_memo_and_the_uncached_walk_agree(repo: Path) -> None:
    run("worktree", "add", "-q", str(repo / ".worktrees/wt"), "-b", "wt", cwd=repo)
    target = repo / ".worktrees/wt/a.md"
    cache: dict[Path, bool] = {}

    first = _under_nested_worktree(target, [repo], cache)
    assert first is _under_nested_worktree(target, [repo], cache)
    assert first is _under_nested_worktree(target, [repo], None)
    assert cache


def build_engine(tmp_path: Path, roots: list[Path]):
    """A real ``IndexEngine`` over a temporary SQLite store, no embedder."""
    from memtomem.config import Mem2MemConfig
    from memtomem.indexing.engine import IndexEngine
    from memtomem.storage.sqlite_backend import SqliteBackend

    config = Mem2MemConfig()
    config.indexing.memory_dirs = [str(r) for r in roots]
    config.storage.sqlite_path = str(tmp_path / "store.db")
    storage = SqliteBackend(config.storage)
    return storage, IndexEngine(storage, None, config.indexing)


def test_the_engine_walk_omits_the_worktree(repo: Path, tmp_path: Path) -> None:
    """End to end through ``discover_indexable_files``, not the predicate alone.

    The predicate tests above all pass even if the engine never consults it;
    this one fails if the wiring is cut.
    """
    run("worktree", "add", "-q", str(repo / ".worktrees/wt"), "-b", "wt", cwd=repo)
    _storage, engine = build_engine(tmp_path, [repo])

    found = engine.discover_indexable_files(repo)

    assert {p.name for p in found} == {"a.md"}
    assert all(".worktrees" not in p.parts for p in found)


def test_the_engine_walk_keeps_a_registered_worktree(repo: Path, tmp_path: Path) -> None:
    """The override, through the engine: both roots configured, both walked."""
    worktree = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(worktree), "-b", "wt", cwd=repo)
    _storage, engine = build_engine(tmp_path, [repo, worktree])

    assert {p.parent.name for p in engine.discover_indexable_files(worktree)} == {"wt"}
    assert {p.name for p in engine.discover_indexable_files(repo)} == {"a.md"}


def test_the_disk_count_does_not_contradict_the_walk_for_a_registered_worktree(
    repo: Path,
) -> None:
    """A registered worktree's file is counted once, under the worktree root.

    The engine indexes it (see the walk test above) and storage attributes it to
    the worktree by longest prefix. Counts are exclusive (#2524), so the parent
    leaves it out whether or not the worktree is registered. Unregistered, it is
    a nested worktree; registered, a more specific root owns it.
    """
    worktree = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(worktree), "-b", "wt", cwd=repo)
    roots = [repo, worktree]

    assert _count_files_on_disk(worktree, frozenset({".md"}), roots) == 1
    assert _count_files_on_disk(repo, frozenset({".md"}), roots) == 1
    assert _count_files_on_disk(repo, frozenset({".md"}), [repo]) == 1


async def test_web_sources_count_puts_a_registered_worktree_under_its_own_root(
    repo: Path, tmp_path: Path
) -> None:
    """Through ``memory_dir_stats``: each root counts only the files it owns.

    Before #2524 the parent's count included the registered worktree's file
    (2), which the Sources tree lists under the worktree group. The call-site
    half of "every root must be passed" is pinned by the nested-root tests in
    ``test_indexing_engine.py``: the worktree case cannot show it, because a
    parent counted alone skips the nested worktree anyway.
    """
    from memtomem.indexing.engine import memory_dir_stats

    worktree = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(worktree), "-b", "wt", cwd=repo)
    storage, _engine = build_engine(tmp_path, [repo, worktree])
    await storage.initialize()
    try:
        rows = await memory_dir_stats(
            storage, [repo, worktree], supported_extensions=frozenset({".md"})
        )
    finally:
        await storage.close()

    by_path = {Path(str(r["path"])).resolve(): r["file_count"] for r in rows}
    assert by_path[repo.resolve()] == 1
    assert by_path[worktree.resolve()] == 1


def test_seed_scale_counts_a_worktree_offered_as_its_own_root(repo: Path) -> None:
    """The helper half of the wizard wiring: every offered root is in view.

    The seed total sums per-root walks, so a nested root offered alongside its
    parent is counted under both — true of any nested root, not only worktrees.
    This pins that a registered worktree follows that rule instead of vanishing.
    """
    from memtomem.cli._index_progress import _collect_seed_scale

    worktree = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(worktree), "-b", "wt", cwd=repo)

    assert _collect_seed_scale(repo, [repo, worktree])[0] == 2
    assert _collect_seed_scale(repo)[0] == 1


def test_mm_init_hands_every_offered_root_to_the_seed_counter(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the ``mm init`` call site: it must pass ``existing``, not count each root alone."""
    from memtomem.cli import init_cmd

    worktree = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(worktree), "-b", "wt", cwd=repo)
    seen: list[tuple[Path, tuple[Path, ...]]] = []

    def record(memory_dir: Path, memory_dirs=()):
        seen.append((memory_dir, tuple(memory_dirs)))
        return 0, 0

    monkeypatch.setattr(init_cmd, "_collect_seed_scale", record)
    init_cmd._maybe_seed_initial_index([repo, worktree], {})

    assert seen == [(repo, (repo, worktree)), (worktree, (repo, worktree))]


# ---------------------------------------------------------------------------
# #2486: walk roots and the enclosing-repository bound
# ---------------------------------------------------------------------------


def _dangling(directory: Path, target: str) -> Path:
    """A directory whose ``.git`` file points at an administration dir that is not there."""
    directory.mkdir(parents=True)
    (directory / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
    (directory / "a.md").write_text("# copy\n", encoding="utf-8")
    return directory


def test_a_walk_root_bounds_an_unowned_walk(repo: Path) -> None:
    wt = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(wt), "-b", "wt", cwd=repo)

    assert _under_nested_worktree(wt / "a.md", [], None, walk_root=repo) is True
    # The walk root is never tested itself: walking the worktree indexes it.
    assert _under_nested_worktree(wt / "a.md", [], None, walk_root=wt) is False


def test_an_owning_root_wins_over_the_walk_root(repo: Path) -> None:
    wt = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(wt), "-b", "wt", cwd=repo)

    assert _under_nested_worktree(wt / "a.md", [repo], None, walk_root=wt) is True


def test_a_walk_root_the_file_is_not_under_falls_back_to_the_repository(
    repo: Path, tmp_path: Path
) -> None:
    wt = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(wt), "-b", "wt", cwd=repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    assert _under_nested_worktree(wt / "a.md", [], None, walk_root=elsewhere) is True


def test_the_engine_explicit_walk_bounds_by_where_it_started(repo: Path, tmp_path: Path) -> None:
    wt = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(wt), "-b", "wt", cwd=repo)
    _storage, engine = build_engine(tmp_path, [])

    from_repo = engine.discover_indexable_files(repo, path_scope="explicit")
    from_worktree = engine.discover_indexable_files(wt, path_scope="explicit")

    assert [p.relative_to(repo.resolve()).as_posix() for p in from_repo] == ["a.md"]
    assert [p.name for p in from_worktree] == ["a.md"]
    assert all(p.is_relative_to(wt.resolve()) for p in from_worktree)


def test_an_explicit_walk_of_a_worktree_a_configured_root_owns_still_skips_it(
    repo: Path, tmp_path: Path
) -> None:
    wt = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(wt), "-b", "wt", cwd=repo)
    _storage, engine = build_engine(tmp_path, [repo])

    assert engine.discover_indexable_files(wt, path_scope="explicit") == []


def test_a_clone_inside_a_worktree_is_its_own_checkout(repo: Path, tmp_path: Path) -> None:
    """The nearest checkout bounds the single-file view; an outer walk still skips it."""
    wt = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(wt), "-b", "wt", cwd=repo)
    clone = wt / "vendor/clone"
    clone.mkdir(parents=True)
    run("init", "-q", ".", cwd=clone)
    (clone / "b.md").write_text("# clone\n", encoding="utf-8")

    assert _under_nested_worktree(clone / "b.md", [], None) is False
    assert _under_nested_worktree(clone / "b.md", [], None, walk_root=repo) is True


def test_an_unrelated_repositorys_worktree_inside_a_checkout_is_skipped(
    repo: Path, tmp_path: Path
) -> None:
    """No membership check under any bound — the accepted consequence, pinned."""
    other = tmp_path / "other"
    other.mkdir()
    run("init", "-q", ".", cwd=other)
    run("config", "user.email", "t@example.invalid", cwd=other)
    run("config", "user.name", "t", cwd=other)
    (other / "o.md").write_text("# other\n", encoding="utf-8")
    run("add", "o.md", cwd=other)
    run("commit", "-qm", "init", cwd=other)
    foreign = repo / "scratch/foreign-wt"
    run("worktree", "add", "-q", str(foreign), "-b", "foreign", cwd=other)

    assert _under_nested_worktree(foreign / "o.md", [], None) is True
    assert _under_nested_worktree(foreign / "o.md", [repo], None) is True
    assert _under_nested_worktree(foreign / "o.md", [], None, walk_root=repo) is True


def test_a_submodule_and_a_nested_clone_stay_indexed_without_a_root(
    tmp_path: Path, repo: Path, spec
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    run("init", "-q", ".", cwd=lib)
    run("config", "user.email", "t@example.invalid", cwd=lib)
    run("config", "user.name", "t", cwd=lib)
    (lib / "l.md").write_text("# lib\n", encoding="utf-8")
    run("add", "l.md", cwd=lib)
    run("commit", "-qm", "init", cwd=lib)
    run("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "sub", cwd=repo)
    nested = repo / "vendor/nested"
    nested.mkdir(parents=True)
    run("init", "-q", ".", cwd=nested)
    (nested / "n.md").write_text("# nested\n", encoding="utf-8")

    assert _path_is_excluded(repo / "sub/l.md", [], spec) is False
    assert _path_is_excluded(nested / "n.md", [], spec) is False


def test_purge_claims_an_unowned_worktree_row_and_nothing_beside_it(
    repo: Path, tmp_path: Path
) -> None:
    from memtomem.cli.purge_cmd import find_sources_matching_excluded

    wt = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(wt), "-b", "wt", cwd=repo)
    top = tmp_path / "top-level-wt"
    run("worktree", "add", "-q", str(top), "-b", "top", cwd=repo)
    nested = repo / "vendor/nested"
    nested.mkdir(parents=True)
    run("init", "-q", ".", cwd=nested)
    (nested / "n.md").write_text("# nested\n", encoding="utf-8")
    sources = [repo / "a.md", wt / "a.md", top / "a.md", nested / "n.md"]

    assert find_sources_matching_excluded(sources, [], []) == [wt / "a.md"]


# ---------------------------------------------------------------------------
# #2487: an administration directory that does not exist here
# ---------------------------------------------------------------------------


def test_a_dangling_worktree_target_is_a_worktree_under_every_bound(
    repo: Path, tmp_path: Path
) -> None:
    """The sandbox case: ``gitdir`` names a ``.git/worktrees/<name>`` from another mount."""
    wt = _dangling(repo / ".worktrees/sandboxed", "/sessions/x/mnt/repo/.git/worktrees/sandboxed")

    assert _is_linked_worktree(wt) is False
    assert _under_nested_worktree(wt / "a.md", [repo], None) is True
    assert _under_nested_worktree(wt / "a.md", [], None, walk_root=repo) is True
    assert _under_nested_worktree(wt / "a.md", [], None) is True


def test_a_relative_dangling_worktree_target_is_a_worktree(repo: Path) -> None:
    wt = _dangling(repo / ".worktrees/rel", "../../gone/.git/worktrees/rel")

    assert _under_nested_worktree(wt / "a.md", [], None) is True


def test_a_dangling_submodule_named_worktrees_is_not_a_worktree(repo: Path) -> None:
    """``.git/modules/worktrees/x``: the grandparent is ``modules``, not ``.git``."""
    sub = _dangling(repo / "worktrees/x", "/nowhere/.git/modules/worktrees/x")

    assert _under_nested_worktree(sub / "a.md", [repo], None) is False
    assert _under_nested_worktree(sub / "a.md", [], None) is False


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="POSIX directory permissions, and root ignores the mode bits",
)
def test_an_unreachable_worktree_target_is_not_absent(repo: Path, tmp_path: Path) -> None:
    """``PermissionError`` from ``lstat`` is not evidence the admin dir is gone."""
    locked = tmp_path / "locked"
    (locked / ".git/worktrees").mkdir(parents=True)
    wt = _dangling(repo / ".worktrees/locked", str(locked / ".git/worktrees/x"))
    locked.chmod(0o000)
    try:
        assert _under_nested_worktree(wt / "a.md", [repo], None) is False
    finally:
        locked.chmod(0o700)


def test_a_nul_in_a_worktree_shaped_target_is_not_absent(repo: Path) -> None:
    # Not ``nul``: that is a reserved device name on Windows, where the
    # directory cannot be created at all (#2484 CI).
    wt = repo / ".worktrees/embedded-nul"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /tmp/a\x00b/.git/worktrees/embedded-nul\n", encoding="utf-8")

    assert _under_nested_worktree(wt / "a.md", [repo], None) is False


# ---------------------------------------------------------------------------
# Normalisation and memo parity
# ---------------------------------------------------------------------------


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a directory symlink here: {exc}")


def test_a_symlinked_ancestor_gives_the_same_answer_everywhere(
    repo: Path, tmp_path: Path, spec
) -> None:
    from memtomem.cli.purge_cmd import find_sources_matching_excluded

    run("worktree", "add", "-q", str(repo / ".worktrees/wt"), "-b", "wt", cwd=repo)
    link = tmp_path / "link"
    _symlink_or_skip(link, repo)
    via_link = link / ".worktrees/wt/a.md"

    assert _under_nested_worktree(via_link, [], None) is True
    assert _under_nested_worktree(via_link, [], None, walk_root=link) is True
    assert find_sources_matching_excluded([via_link], [], []) == [via_link]


def test_a_link_into_a_worktree_from_outside_any_checkout_is_resolved_first(
    repo: Path, tmp_path: Path
) -> None:
    """Lexically this path has no enclosing checkout; physically it is inside ``repo``.

    Judging the lexical ancestry would index the copy through the link while purge,
    handed the resolved path the store keeps, claimed it.
    """
    from memtomem.cli.purge_cmd import find_sources_matching_excluded

    run("worktree", "add", "-q", str(repo / ".worktrees/wt"), "-b", "wt", cwd=repo)
    plain = tmp_path / "plain"
    plain.mkdir()
    _symlink_or_skip(plain / "link", repo / ".worktrees/wt")
    via_link = plain / "link/a.md"

    assert _under_nested_worktree(via_link, [], None) is True
    assert find_sources_matching_excluded([via_link], [], []) == [via_link]


def test_a_symlinked_git_directory_still_marks_a_checkout(tmp_path: Path) -> None:
    real_git = tmp_path / "gitdir-elsewhere"
    real_git.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _symlink_or_skip(checkout / ".git", real_git)
    wt = _dangling(checkout / ".worktrees/x", "/gone/.git/worktrees/x")

    assert _under_nested_worktree(wt / "a.md", [], None) is True


@pytest.mark.parametrize("bound", ["root", "walk", "repository"])
def test_the_memo_agrees_with_the_uncached_answer_for_every_bound(repo: Path, bound: str) -> None:
    wt = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(wt), "-b", "wt", cwd=repo)
    roots = [repo] if bound == "root" else []
    walk_root = repo if bound == "walk" else None
    memo: dict = {}

    for target in (wt / "a.md", repo / "a.md"):
        uncached = _under_nested_worktree(target, roots, None, walk_root=walk_root)
        assert _under_nested_worktree(target, roots, memo, walk_root=walk_root) is uncached
        assert _under_nested_worktree(target, roots, memo, walk_root=walk_root) is uncached
    assert memo


# ---------------------------------------------------------------------------
# End to end: the per-file guard agrees with the walk (#2486)
# ---------------------------------------------------------------------------


@pytest.fixture
async def outside_repo(bm25_only_components, tmp_path):
    """A repository with a worktree, outside the configured memory dir."""
    comp, _mem_dir = bm25_only_components
    root = tmp_path / "outside"
    root.mkdir()
    run("init", "-q", ".", cwd=root)
    run("config", "user.email", "t@example.invalid", cwd=root)
    run("config", "user.name", "t", cwd=root)
    (root / "a.md").write_text("# main\n\nmain body\n", encoding="utf-8")
    run("add", "a.md", cwd=root)
    run("commit", "-qm", "init", cwd=root)
    wt = root / ".worktrees/wt"
    run("worktree", "add", "-q", str(wt), "-b", "wt", cwd=root)
    return comp, root, wt


async def _stored_sources(comp) -> set[Path]:
    return {Path(p).resolve() for p in await comp.storage.get_all_source_files()}


async def test_index_path_of_the_repository_stores_no_worktree_copy(outside_repo) -> None:
    comp, root, wt = outside_repo

    stats = await comp.index_engine.index_path(root, path_scope="explicit")

    assert not stats.errors
    assert await _stored_sources(comp) == {(root / "a.md").resolve()}


async def test_index_path_of_the_worktree_itself_stores_it(outside_repo) -> None:
    """The walk kept the file, so the per-file guard must too — it gets the walk root."""
    comp, _root, wt = outside_repo

    stats = await comp.index_engine.index_path(wt, path_scope="explicit")

    assert not stats.errors
    assert await _stored_sources(comp) == {(wt / "a.md").resolve()}


async def test_a_single_worktree_file_is_skipped(outside_repo) -> None:
    comp, _root, wt = outside_repo

    stats = await comp.index_engine.index_file(wt / "a.md", path_scope="explicit")

    assert stats.total_chunks == 0
    assert await _stored_sources(comp) == set()


async def _drain_stream(comp, path: Path) -> None:
    async for event in comp.index_engine.index_path_stream(path, path_scope="explicit"):
        if event.get("type") == "complete":
            assert not event["errors"], event["errors"]


async def test_a_streamed_walk_of_the_worktree_stores_it(outside_repo) -> None:
    comp, _root, wt = outside_repo

    await _drain_stream(comp, wt)

    assert await _stored_sources(comp) == {(wt / "a.md").resolve()}


async def test_a_streamed_single_worktree_file_is_skipped(outside_repo) -> None:
    comp, _root, wt = outside_repo

    await _drain_stream(comp, wt / "a.md")

    assert await _stored_sources(comp) == set()


async def test_the_debounce_drain_skips_an_edited_worktree_file(outside_repo) -> None:
    """A hook-fed edit reaches ``index_file`` with no walk, so the repository bounds it."""
    from memtomem.cli.indexing import _make_indexer

    comp, _root, wt = outside_repo
    indexer = _make_indexer(comp)

    assert await indexer(str(wt / "a.md"), None, False) == "skipped"
    assert await _stored_sources(comp) == set()


# ---------------------------------------------------------------------------
# budget_audit classifies unowned sources the same way
# ---------------------------------------------------------------------------


def _audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sources: list[Path]) -> dict:
    """Run ``budget_audit.audit`` over a minimal store holding one chunk per source."""
    import sqlite3

    from memtomem.config import IndexingConfig
    from memtomem.embedding import profiles
    from memtomem.indexing.budget_audit import audit

    tokenizers = pytest.importorskip("tokenizers")
    tokenizer = tokenizers.Tokenizer(tokenizers.models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    tokenizer_path = tmp_path / "tokenizer.json"
    tokenizer.save(str(tokenizer_path))
    monkeypatch.setattr(profiles, "resolve_tokenizer", lambda identifier: tokenizer_path)
    db_path = tmp_path / "audit.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE chunks (id TEXT, source_file TEXT, content TEXT, content_hash TEXT, "
            "chunk_type TEXT, namespace TEXT, scope TEXT, heading_hierarchy TEXT, "
            "retrieval_context TEXT, start_line INTEGER)"
        )
        for n, source in enumerate(sources):
            db.execute(
                "INSERT INTO chunks VALUES (?, ?, 'x = 1', 'h', 'code', 'default', 'user', "
                "'[]', '', 1)",
                (f"id{n}", str(source)),
            )
    config = IndexingConfig(
        hard_max_chunk_tokens=64,
        chunk_tokenizer_path=str(tokenizer_path),
        chunk_context_tokens=64,
        chunk_model_tokens=512,
        memory_dirs=[],
    )
    return audit(db_path, config, set())


def test_budget_audit_reports_an_unowned_worktree_row_as_excluded(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(wt), "-b", "wt", cwd=repo)
    (repo / "m.py").write_text("x = 1\n", encoding="utf-8")
    (wt / "m.py").write_text("x = 1\n", encoding="utf-8")

    report = _audit(tmp_path, monkeypatch, [repo / "m.py", wt / "m.py"])

    assert [e["source"] for e in report["excluded"]] == [str(wt / "m.py")]
    assert [e["source"] for e in report["reindex"]] == [str(repo / "m.py")]
    assert report["complete"] is True


# ---------------------------------------------------------------------------
# A path that cannot be resolved: the predicate raises, batch callers skip the row
# ---------------------------------------------------------------------------

UNRESOLVABLE = "unresolvable-source"


@pytest.fixture(params=[ValueError, RuntimeError, OSError])
def unresolvable(request, monkeypatch: pytest.MonkeyPatch) -> type[Exception]:
    """``Path.resolve`` raises for one path, the way a NUL or a symlink loop does."""
    real = Path.resolve
    error = request.param

    def resolve(self, strict=False):
        if UNRESOLVABLE in str(self):
            raise error("cannot resolve")
        return real(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    return error


@pytest.mark.parametrize("roots", [[], ["root"]])
def test_the_predicate_raises_rather_than_answering_not_excluded(
    unresolvable, tmp_path: Path, spec, roots
) -> None:
    """ "Not excluded" on failure would be fail-open for the secret denylist."""
    target = tmp_path / UNRESOLVABLE / "a.md"
    memory_dirs = [tmp_path] if roots else []

    with pytest.raises(unresolvable):
        _path_is_excluded(target, memory_dirs, spec)


def test_a_denylisted_file_is_not_indexable_beside_an_unresolvable_root(
    tmp_path: Path, spec
) -> None:
    """Review round 2's reproduction: a looping unrelated root flipped ``id_rsa.md`` to indexable.

    Where the interpreter can resolve the loop the file is excluded; where it
    raises (``RuntimeError`` on 3.12) the predicate raises too. Neither may answer False.
    """
    looping = tmp_path / "looping"
    looping.mkdir()
    try:
        (looping / "a").symlink_to(looping / "b", target_is_directory=True)
        (looping / "b").symlink_to(looping / "a", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink loop here: {exc}")
    notes = tmp_path / "notes"
    notes.mkdir()
    secret = notes / "id_rsa.md"
    secret.write_text("-----BEGIN KEY-----\n", encoding="utf-8")

    try:
        answer = _path_is_excluded(secret, [looping / "a" / "root", notes], spec)
    except (OSError, ValueError, RuntimeError):
        return
    assert answer is True


def test_purge_classifies_past_an_unresolvable_row_and_reports_it(unresolvable, repo: Path) -> None:
    from memtomem.cli.purge_cmd import classify_sources_for_purge

    wt = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(wt), "-b", "wt", cwd=repo)
    broken = repo / UNRESOLVABLE / "a.md"

    scan = classify_sources_for_purge([broken, wt / "a.md"], [], [repo])

    assert scan.matched == [wt / "a.md"]
    assert scan.unclassified == [broken]


def test_budget_audit_passes_over_an_unresolvable_row(
    unresolvable, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(wt), "-b", "wt", cwd=repo)
    (wt / "m.py").write_text("x = 1\n", encoding="utf-8")
    broken = repo / UNRESOLVABLE / "m.py"

    report = _audit(tmp_path, monkeypatch, [broken, wt / "m.py"])

    assert [e["source"] for e in report["excluded"]] == [str(wt / "m.py")]
    assert report["unclassified_sources"] == [str(broken)]
    assert report["complete"] is False
    assert str(broken) not in {e["source"] for e in report["reindex"]}
