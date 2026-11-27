"""#3623: ``resolve_project_tier_write_dir``, the resolver project-tier writes use.

It is :func:`~memtomem.memory_scope.resolve_memory_scope_dir` with one more
refusal: a project tier whose directory, or the ``.memtomem`` holding it, is a
link or cannot be inspected. The surfaces that call it are pinned in
``test_project_tier_relinked_writes.py``, which does not import it, so that file
also runs against a revision without it.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from memtomem.memory_scope import (
    MemoryScopeError,
    resolve_memory_scope_dir,
    resolve_project_tier_write_dir,
)

_TIER_LEAF = {"project_shared": "memories", "project_local": "memories.local"}


def _link_dir(link: Path, target: Path) -> None:
    link.symlink_to(target, target_is_directory=True)


def _fail_resolving(monkeypatch, tier: Path, exc: BaseException) -> None:
    """Make ``Path.resolve`` raise ``exc`` for ``tier`` exactly, as a loop does on 3.12."""
    real_resolve = Path.resolve

    def _resolve(self, *args, **kwargs):
        if self == tier:
            raise exc
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)


def test_the_write_resolver_matches_the_plain_one_on_a_regular_tier(tmp_path):
    root = tmp_path / "proj"
    for leaf in ("memories", "memories.local"):
        (root / ".memtomem" / leaf).mkdir(parents=True)
    for scope in ("project_shared", "project_local"):
        assert resolve_project_tier_write_dir(scope, root) == resolve_memory_scope_dir(scope, root)
    # A tier that does not exist yet is not a link.
    fresh = tmp_path / "fresh"
    assert resolve_project_tier_write_dir("project_local", fresh) == resolve_memory_scope_dir(
        "project_local", fresh
    )


def test_the_write_resolver_keeps_the_plain_resolvers_other_answers(tmp_path):
    user_base = tmp_path / "user"
    assert resolve_project_tier_write_dir("user", None, user_base) == resolve_memory_scope_dir(
        "user", None, user_base
    )
    with pytest.raises(MemoryScopeError, match="requires a registered project context"):
        resolve_project_tier_write_dir("project_local", None)
    with pytest.raises(MemoryScopeError, match="unsupported memory scope"):
        resolve_project_tier_write_dir("elsewhere", tmp_path)  # type: ignore[arg-type]
    with pytest.raises(MemoryScopeError, match="unsupported memory scope"):
        resolve_project_tier_write_dir([], tmp_path)  # type: ignore[arg-type]


@pytest.mark.requires_symlinks
@pytest.mark.parametrize(
    ("linked", "scope", "entry"),
    [
        ("tier", "project_local", ".memtomem/memories.local"),
        ("tier", "project_shared", ".memtomem/memories"),
        ("dot", "project_local", ".memtomem/memories.local"),
    ],
)
def test_the_write_resolver_refuses_either_linked_entry(tmp_path, linked, scope, entry):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "memories").mkdir(parents=True)
    (elsewhere / "memories.local").mkdir()
    root = tmp_path / "proj"
    if linked == "dot":
        root.mkdir()
        _link_dir(root / ".memtomem", elsewhere)
    else:
        (root / ".memtomem").mkdir(parents=True)
        leaf = entry.rsplit("/", 1)[1]
        _link_dir(root / ".memtomem" / leaf, elsewhere / leaf)

    with pytest.raises(MemoryScopeError) as exc_info:
        resolve_project_tier_write_dir(scope, root)

    assert str(exc_info.value).startswith(f"{entry}, or the .memtomem directory holding it, ")
    assert str(tmp_path) not in str(exc_info.value)


@pytest.mark.requires_symlinks
def test_the_write_resolver_refuses_a_tier_link_that_loops(tmp_path):
    """Judged by ``lstat`` before anything resolves the loop."""
    root = tmp_path / "proj"
    (root / ".memtomem").mkdir(parents=True)
    loop = root / ".memtomem" / "memories.local"
    loop.symlink_to(loop)

    with pytest.raises(MemoryScopeError, match="symbolic link or junction"):
        resolve_project_tier_write_dir("project_local", root)


@pytest.mark.parametrize("scope", ["project_local", "project_shared"])
@pytest.mark.parametrize("kind", ["runtime", "oserror"])
def test_a_tier_that_fails_to_resolve_after_the_check_is_refused_without_its_path(
    tmp_path, monkeypatch, scope, kind
):
    """#3646: a tier swapped for a loop after ``lstat`` fails in ``resolve()``.

    Python 3.12 raises ``RuntimeError`` naming the absolute path; the failure
    is injected so every interpreter runs this case. The refusal is the
    linked-tier one, and the original exception is not chained onto it.
    """
    root = tmp_path / "proj"
    tier = root / ".memtomem" / _TIER_LEAF[scope]
    tier.mkdir(parents=True)
    if kind == "runtime":
        exc: BaseException = RuntimeError(f"Symlink loop from '{tier}'")
    else:
        exc = OSError(errno.ELOOP, "Too many levels of symbolic links", str(tier))
    _fail_resolving(monkeypatch, tier, exc)

    with pytest.raises(MemoryScopeError) as exc_info:
        resolve_project_tier_write_dir(scope, root)

    message = str(exc_info.value)
    assert message.startswith(f".memtomem/{_TIER_LEAF[scope]}, or the .memtomem directory ")
    assert "could not be inspected; nothing was written" in message
    assert str(tmp_path) not in message
    assert tmp_path.as_posix() not in message
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__


@pytest.mark.requires_symlinks
@pytest.mark.parametrize("looped", ["tier", "dot"])
def test_a_tier_that_becomes_a_loop_after_the_check_raises_nothing_else(
    tmp_path, monkeypatch, looped
):
    """The race of #3646 with a real loop: the check is patched to have passed.

    Where ``resolve()`` raises on a loop (3.12) the answer is the refusal;
    where it returns the spelled path (3.13+) that path comes back. No other
    exception, and no other path, is an answer.
    """
    monkeypatch.setattr("memtomem.memory_scope.project_tier_is_relinked", lambda _tier: False)
    root = tmp_path / "proj"
    if looped == "dot":
        root.mkdir()
        (root / ".memtomem").symlink_to(root / ".memtomem")
    else:
        (root / ".memtomem").mkdir(parents=True)
        (root / ".memtomem" / "memories.local").symlink_to(root / ".memtomem" / "memories.local")

    try:
        result = resolve_project_tier_write_dir("project_local", root)
    except MemoryScopeError as exc:
        assert "could not be inspected; nothing was written" in str(exc)
        assert str(tmp_path) not in str(exc)
    else:
        assert result == root.resolve() / ".memtomem" / "memories.local"


def test_the_write_resolver_refuses_a_tier_it_cannot_inspect(tmp_path, monkeypatch):
    """An entry ``lstat`` fails on counts as a link (``entry_is_link``)."""
    root = tmp_path / "proj"
    tier = root / ".memtomem" / "memories.local"
    tier.mkdir(parents=True)
    real_lstat = Path.lstat

    def _lstat(self):
        if self == tier:
            raise PermissionError(13, "denied", str(self))
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _lstat)

    with pytest.raises(MemoryScopeError, match="could not be inspected"):
        resolve_project_tier_write_dir("project_local", root)


@pytest.mark.requires_symlinks
def test_a_link_above_the_project_root_is_not_judged(tmp_path):
    """The path above the project root is the machine's (``/var`` on macOS)."""
    real = tmp_path / "real"
    (real / "proj" / ".memtomem" / "memories.local").mkdir(parents=True)
    alias = tmp_path / "alias"
    _link_dir(alias, real)
    root = alias / "proj"

    assert resolve_project_tier_write_dir("project_local", root) == resolve_memory_scope_dir(
        "project_local", root
    )
