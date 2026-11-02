"""A walk normalises each configured root once, not once per file (#2966).

``IndexRoots`` memoises the two per-root facts the exclusion check needs. The
contract pinned here: every answer and every exception is the one the plain
list of roots gives; failures are never cached; and the memo actually reaches
every hop of discovery, the bulk run and the stream run.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import unicodedata
from contextlib import contextmanager
from pathlib import Path

import pytest

from memtomem.indexing import dir_stats as dir_stats_mod
from memtomem.indexing import engine as engine_mod
from memtomem.indexing.engine import (
    IndexRoots,
    _build_exclude_spec,
    _exclude_match_keys,
    _path_is_excluded,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, timeout=60)


@pytest.fixture
def spec():
    return _build_exclude_spec([])


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Roots of every shape the ownership and key layers distinguish.

    ``HOME`` is ``tmp_path`` so ``tilde_repo`` names the repository by an
    unexpanded ``~`` spelling: the memo keys the configured spelling and the
    worktree bound's expanded owner separately, and both must answer alike.
    Windows' ``ntpath.expanduser`` reads ``USERPROFILE``, not ``HOME``.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    if shutil.which("git") is None:
        pytest.skip("needs a git binary")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", ".", cwd=repo)
    _git("config", "user.email", "t@example.invalid", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "a.md").write_text("# a\n", encoding="utf-8")
    _git("add", "a.md", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)
    wt = repo / ".worktrees" / "wt"
    _git("worktree", "add", "-q", str(wt), "-b", "wt", cwd=repo)

    nested = repo / "sub"
    nested.mkdir()
    (nested / "n.md").write_text("# n\n", encoding="utf-8")
    (nested / "id_rsa.md").write_text("k\n", encoding="utf-8")

    memory = tmp_path / ".claude" / "projects" / "p" / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("- [x](x.md)\n", encoding="utf-8")
    (memory / "x.md").write_text("# x\n", encoding="utf-8")

    real = tmp_path / "real"
    real.mkdir()
    (real / "r.md").write_text("# r\n", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        link = real

    nfd = tmp_path / unicodedata.normalize("NFD", "café")
    nfd.mkdir()
    (nfd / "c.md").write_text("# c\n", encoding="utf-8")

    orphan_dir = tmp_path / "orphan"
    orphan_dir.mkdir()
    (orphan_dir / "o.md").write_text("# o\n", encoding="utf-8")

    return {
        "repo": repo,
        "tilde_repo": Path("~") / "repo",
        "wt": wt,
        "nested": nested,
        "memory": memory,
        "link": link,
        "nfd": nfd,
        "missing": tmp_path / "missing",
        "orphan_dir": orphan_dir,
        "elsewhere": tmp_path,
    }


def _files(t: dict[str, Path]) -> list[Path]:
    return [
        t["repo"] / "a.md",
        t["wt"] / "a.md",
        t["nested"] / "n.md",
        t["nested"] / "id_rsa.md",
        t["memory"] / "MEMORY.md",
        t["memory"] / "x.md",
        t["link"] / "r.md",
        t["nfd"] / "c.md",
        t["orphan_dir"] / "o.md",
    ]


#: Patterns anchored to a root match only a root-relative key — the absolute
#: path never starts with ``r.md`` — so the matrix fails if those keys go.
ANCHORED = ["/r.md", "/c.md", "/n.md"]


@pytest.mark.parametrize(
    "walk",
    [None, "repo", "wt", "orphan_dir", "elsewhere"],
    ids=["no-walk-root", "walk-repo", "walk-worktree", "walk-unrelated", "walk-above"],
)
@pytest.mark.parametrize(
    "root_names",
    [
        ["repo", "nested", "memory", "link", "nfd", "missing"],
        ["nested", "repo", "memory"],
        ["wt", "repo"],
        ["tilde_repo", "link", "nfd"],
        [],
    ],
    ids=["all", "nested-first", "worktree-registered", "tilde-spelling", "no-roots"],
)
@pytest.mark.parametrize("patterns", [[], ANCHORED], ids=["no-user-patterns", "anchored"])
def test_the_memo_answers_what_the_plain_roots_answer(tree, walk, root_names, patterns) -> None:
    roots = [tree[n] for n in root_names]
    walk_root = tree[walk] if walk else None
    user_spec = _build_exclude_spec(patterns)
    memo = IndexRoots(roots)  # one memo across every file, as a walk shares it
    for f in _files(tree):
        assert _exclude_match_keys(f, memo) == _exclude_match_keys(f, roots), f
        plain = _path_is_excluded(f, roots, user_spec, worktree_cache={}, walk_root=walk_root)
        cached = _path_is_excluded(f, memo, user_spec, worktree_cache={}, walk_root=walk_root)
        assert cached is plain, f


def test_the_matrix_reaches_every_layer(tree, spec) -> None:
    """Guard against a vacuous matrix: each exclusion layer answers True somewhere."""
    roots = [tree[n] for n in ("repo", "nested", "memory", "link", "nfd")]
    memo = IndexRoots(roots)
    assert _path_is_excluded(tree["memory"] / "MEMORY.md", memo, spec) is True  # provider
    assert _path_is_excluded(tree["nested"] / "id_rsa.md", memo, spec) is True  # denylist
    assert _path_is_excluded(tree["wt"] / "a.md", memo, spec) is True  # worktree
    assert _path_is_excluded(tree["memory"] / "x.md", memo, spec) is False
    anchored = _build_exclude_spec(ANCHORED)
    assert _path_is_excluded(tree["link"] / "r.md", memo, anchored) is True  # rel key
    assert _path_is_excluded(tree["link"] / "r.md", IndexRoots([]), anchored) is False
    tilde = IndexRoots([tree["tilde_repo"]])
    assert _path_is_excluded(tree["wt"] / "a.md", tilde, spec) is True  # expanded owner


# ── failures are never cached ────────────────────────────────────────


@contextmanager
def _resolve_raises_for(monkeypatch: pytest.MonkeyPatch, poisoned: Path, exc_factory):
    original = pathlib.Path.resolve

    def resolve(self, strict=False):
        if self == poisoned:
            raise exc_factory()
        return original(self, strict=strict)

    with monkeypatch.context() as m:
        m.setattr(pathlib.Path, "resolve", resolve)
        yield


def _chained() -> RuntimeError:
    try:
        raise OSError(40, "Too many levels of symbolic links", "/loop")
    except OSError as cause:
        try:
            raise RuntimeError("injected loop") from cause
        except RuntimeError as exc:
            return exc


def _denied() -> OSError:
    return OSError(13, "Permission denied", "/denied")


@pytest.mark.parametrize("make", [_chained, _denied], ids=["loop", "oserror"])
def test_an_unresolvable_root_raises_for_every_file_with_its_own_exception(
    tree, spec, monkeypatch: pytest.MonkeyPatch, make
) -> None:
    """The fail-closed case: ownership absorbs the failure, the key layer raises.

    ``norm_path`` falls back on ``OSError`` and ``resolve_path`` on 3.12's loop
    ``RuntimeError``, so ownership answers; the key layer's plain ``resolve``
    is the raise, for every file that reaches it.
    """
    poisoned = tree["missing"]
    roots = [tree["repo"], poisoned, tree["memory"]]
    memo = IndexRoots(roots)
    a, b = tree["repo"] / "a.md", tree["memory"] / "x.md"
    kind = type(make())

    with _resolve_raises_for(monkeypatch, poisoned, make):
        with pytest.raises(kind) as plain:
            _path_is_excluded(a, roots, spec)
        with pytest.raises(kind) as first:
            _path_is_excluded(a, memo, spec)
        with pytest.raises(kind) as second:
            _path_is_excluded(b, memo, spec)

    for got in (first.value, second.value):
        assert got.args == plain.value.args
        if issubclass(kind, OSError):
            assert (got.errno, got.filename) == (13, "/denied")
        else:
            assert isinstance(got.__cause__, OSError)
            assert (got.__cause__.errno, got.__cause__.filename) == (40, "/loop")
    assert first.value is not second.value
    assert poisoned not in memo._resolved


def test_a_provider_file_short_circuits_before_an_unresolvable_root(
    tree, spec, monkeypatch: pytest.MonkeyPatch
) -> None:
    poisoned = tree["missing"]
    roots = [tree["memory"], poisoned]
    memo = IndexRoots(roots)
    with _resolve_raises_for(monkeypatch, poisoned, _chained):
        assert _path_is_excluded(tree["memory"] / "MEMORY.md", roots, spec) is True
        assert _path_is_excluded(tree["memory"] / "MEMORY.md", memo, spec) is True


def _outcome(fn):
    try:
        return ("ok", fn())
    except Exception as exc:  # noqa: BLE001 — the outcome itself is under test
        return ("raise", type(exc), exc.args)


def test_a_root_whose_resolution_is_a_value_error_behaves_as_before(
    tree, spec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``ValueError`` from resolving a root escapes ownership (``norm_path``
    catches only ``OSError``) before the key layer's own ``except ValueError``
    is reached; whichever layer answers, memo and plain list answer alike."""
    poisoned = tree["missing"]
    roots = [tree["repo"], poisoned, tree["nested"]]
    memo = IndexRoots(roots)
    with _resolve_raises_for(monkeypatch, poisoned, lambda: ValueError("embedded null byte")):
        for f in (tree["nested"] / "id_rsa.md", tree["nested"] / "n.md"):
            plain = _outcome(lambda: _path_is_excluded(f, roots, spec))
            assert _outcome(lambda: _path_is_excluded(f, memo, spec)) == plain
    assert poisoned not in memo._resolved
    assert poisoned not in memo._prefixes


@pytest.mark.parametrize("make", [lambda: OSError(13, "denied"), lambda: ValueError("x"), _chained])
def test_a_walk_root_that_cannot_be_resolved_excludes_nothing_from_the_worktree_layer(
    tree, spec, monkeypatch: pytest.MonkeyPatch, make
) -> None:
    """The bound's own handler, reached the only way a consistent filesystem reaches it.

    An *owning* root that fails to resolve fails in ownership or the key layer
    first, which resolve the same path before the worktree layer runs; the walk
    root is resolved only by the worktree layer.
    """
    wt = tree["wt"]
    memo = IndexRoots([])
    assert _path_is_excluded(wt / "a.md", memo, spec, walk_root=tree["repo"]) is True
    with _resolve_raises_for(monkeypatch, tree["repo"], make):
        assert _path_is_excluded(wt / "a.md", [], spec, walk_root=tree["repo"]) is False
        assert _path_is_excluded(wt / "a.md", memo, spec, walk_root=tree["repo"]) is False


UNKNOWN_USER_ROOT = Path("~memtomem-no-such-user-2966/notes")


@pytest.fixture
def unusable_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``UNKNOWN_USER_ROOT.expanduser()`` raise on every platform.

    POSIX raises ``RuntimeError`` for an unknown ``~user`` by itself; Windows'
    ``ntpath.expanduser`` instead answers with a sibling of the home directory,
    so the failure is injected rather than left to the platform.
    """
    original = pathlib.Path.expanduser

    def expanduser(self):
        if self == UNKNOWN_USER_ROOT:
            raise RuntimeError("Could not determine home directory.")
        return original(self)

    monkeypatch.setattr(pathlib.Path, "expanduser", expanduser)


@pytest.mark.parametrize("content", ["empty", "prefiltered"])
async def test_an_unusable_root_does_not_fail_a_walk_that_checks_nothing(
    bm25_only_components, unusable_root, tmp_path: Path, content: str
) -> None:
    """Nothing is resolved at construction: a walk with no candidate stays ``[]``.

    ``~unknown-user`` makes ``expanduser`` raise ``RuntimeError``; before the
    memo an empty or fully prefiltered walk never asked, and it still must not.
    """
    comp, _mem_dir = bm25_only_components
    engine = comp.index_engine
    engine._config.memory_dirs = [UNKNOWN_USER_ROOT]
    walked = tmp_path / "walked"
    walked.mkdir()
    if content == "prefiltered":
        (walked / "blob.bin").write_bytes(b"\0")

    assert engine.discover_indexable_files(walked, path_scope="explicit") == []
    stats = await engine.index_path(walked, path_scope="explicit")
    assert (stats.total_files, stats.errors) == (0, ())
    events = [e async for e in engine.index_path_stream(walked, path_scope="explicit")]
    assert events[-1]["type"] == "complete"
    assert events[-1]["total_files"] == 0


async def test_an_unusable_root_still_fails_a_walk_that_checks_a_file(
    bm25_only_components, unusable_root, tmp_path: Path
) -> None:
    comp, _mem_dir = bm25_only_components
    engine = comp.index_engine
    engine._config.memory_dirs = [UNKNOWN_USER_ROOT]
    walked = tmp_path / "walked"
    walked.mkdir()
    (walked / "a.md").write_text("# a\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        engine.discover_indexable_files(walked, path_scope="explicit")


# ── the memo reaches every hop ───────────────────────────────────────


@contextmanager
def _count_exclusion_resolves(monkeypatch: pytest.MonkeyPatch):
    """Count ``Path.resolve`` calls made while ``_path_is_excluded`` runs.

    Wraps the module global both discovery and the per-file guard call, so a
    hop that stops passing the walk's memo shows up as per-file root cost.
    """
    count = [0]
    inside = [0]
    original_resolve = pathlib.Path.resolve
    original_excluded = engine_mod._path_is_excluded

    def resolve(self, strict=False):
        if inside[0]:
            count[0] += 1
        return original_resolve(self, strict=strict)

    def excluded(*args, **kwargs):
        inside[0] += 1
        try:
            return original_excluded(*args, **kwargs)
        finally:
            inside[0] -= 1

    with monkeypatch.context() as m:
        m.setattr(pathlib.Path, "resolve", resolve)
        m.setattr(engine_mod, "_path_is_excluded", excluded)
        yield count


N_FILES = 40
EXTRA_ROOTS = 10


def _populate(mem_dir: Path, tmp_path: Path) -> list[Path]:
    for i in range(N_FILES):
        (mem_dir / f"note{i}.md").write_text(f"# note {i}\n\nbody {i}\n", encoding="utf-8")
    extras = []
    for i in range(EXTRA_ROOTS):
        extra = tmp_path / f"extra{i}"
        extra.mkdir()
        extras.append(extra)
    return extras


async def _run(engine, entry: str, mem_dir: Path) -> None:
    if entry == "discover":
        assert len(engine.discover_indexable_files(mem_dir)) == N_FILES
    elif entry == "index_path":
        stats = await engine.index_path(mem_dir, force=True)
        assert stats.total_files == N_FILES
    else:
        events = [e async for e in engine.index_path_stream(mem_dir, force=True)]
        assert events[-1]["total_files"] == N_FILES


@pytest.mark.parametrize("entry", ["discover", "index_path", "index_path_stream"])
async def test_extra_roots_cost_once_per_walk_not_once_per_file(
    bm25_only_components, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    comp, mem_dir = bm25_only_components
    engine = comp.index_engine
    extras = _populate(mem_dir, tmp_path)

    engine._config.memory_dirs = [mem_dir]
    with _count_exclusion_resolves(monkeypatch) as few:
        await _run(engine, entry, mem_dir)
    engine._config.memory_dirs = [mem_dir, *extras]
    with _count_exclusion_resolves(monkeypatch) as many:
        await _run(engine, entry, mem_dir)

    # Per walk each extra root is resolved a bounded number of times (its
    # prefix and its plain path); a hop without the memo resolves every extra
    # root once per file there, N_FILES * EXTRA_ROOTS more.
    extra_cost = many[0] - few[0]
    assert extra_cost <= 3 * EXTRA_ROOTS, (few[0], many[0])


@pytest.mark.parametrize("entry", ["discover", "index_path", "index_path_stream"])
async def test_the_owning_root_and_ownership_are_worked_out_once_per_check(
    bm25_only_components, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """What the extra-root delta cannot see: costs paid for the root every file shares."""
    comp, mem_dir = bm25_only_components
    engine = comp.index_engine
    for i in range(N_FILES):
        (mem_dir / f"note{i}.md").write_text(f"# note {i}\n", encoding="utf-8")
    engine._config.memory_dirs = [mem_dir]

    owner_resolves = [0]
    ownership_calls = [0]
    checks = [0]
    original_resolve = pathlib.Path.resolve
    original_owner = dir_stats_mod.resolve_owning_memory_dir
    original_excluded = engine_mod._path_is_excluded

    def resolve(self, strict=False):
        if self == mem_dir:
            owner_resolves[0] += 1
        return original_resolve(self, strict=strict)

    def owner(*args, **kwargs):
        ownership_calls[0] += 1
        return original_owner(*args, **kwargs)

    def excluded(*args, **kwargs):
        checks[0] += 1
        return original_excluded(*args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(pathlib.Path, "resolve", resolve)
        m.setattr(dir_stats_mod, "resolve_owning_memory_dir", owner)
        m.setattr(engine_mod, "resolve_owning_memory_dir", owner)
        m.setattr(engine_mod, "_path_is_excluded", excluded)
        await _run(engine, entry, mem_dir)

    assert checks[0] >= N_FILES
    assert ownership_calls[0] == checks[0]  # not a second lookup in the worktree layer
    assert owner_resolves[0] < N_FILES  # the owning root is not resolved per file


async def test_the_guard_uses_fresh_roots_when_the_config_moved_mid_walk(
    bm25_only_components, tmp_path: Path
) -> None:
    """A root added after the walk memoised its roots is honoured at the guard."""
    comp, mem_dir = bm25_only_components
    engine = comp.index_engine
    memory = tmp_path / ".claude" / "projects" / "p" / "memory"
    memory.mkdir(parents=True)
    toc = memory / "MEMORY.md"
    toc.write_text("- [x](x.md)\n", encoding="utf-8")

    stale = IndexRoots([mem_dir])
    engine._config.memory_dirs = [mem_dir, memory]
    result = await engine._index_file(toc, False, index_roots=stale)

    # Judged with the stale memo, MEMORY.md would be an orphan and indexed.
    assert result == {"total": 0, "indexed": 0, "skipped": 0, "deleted": 0, "errors": []}
