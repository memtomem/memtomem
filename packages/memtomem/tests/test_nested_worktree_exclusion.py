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


def test_a_file_with_no_owning_root_is_not_checked(repo: Path, spec) -> None:
    run("worktree", "add", "-q", str(repo / ".worktrees/wt"), "-b", "wt", cwd=repo)

    assert _under_nested_worktree(repo / ".worktrees/wt/a.md", [], None) is False


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
    """Counting with the parent root alone subtracted a file the engine indexes."""
    worktree = repo / ".worktrees/wt"
    run("worktree", "add", "-q", str(worktree), "-b", "wt", cwd=repo)

    # With every root in view the worktree owns itself, so its file counts.
    assert _count_files_on_disk(repo, frozenset({".md"}), [repo, worktree]) == 2
    # With the parent alone it is a nested worktree and does not.
    assert _count_files_on_disk(repo, frozenset({".md"}), [repo]) == 1


async def test_web_sources_count_keeps_a_registered_worktree_in_its_parent(
    repo: Path, tmp_path: Path
) -> None:
    """Pins the call site, not just the helper: ``memory_dir_stats`` must pass the root list.

    ``_count_files_on_disk`` counts correctly when handed every root; a call
    site that hands it only its own root still passes every helper test, and
    the Sources tab then reads ``source_file_count=2`` beside ``file_count=1``.
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
    assert by_path[repo.resolve()] == 2
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
