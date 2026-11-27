"""A project tier that cannot be resolved does not break Pinned Context reads (#3670).

Python 3.12's ``Path.resolve()`` raises ``RuntimeError("Symlink loop from
'<absolute path>'")`` for a symlink loop; 3.13+ returns a path instead (for a
tier that is a link to itself under a resolved parent, its spelled path). The
store derives a project tier by resolving it, so on 3.12 a looping
``.memtomem/memories.local`` made ``list()`` (every compose) and
``search_exclusion_roots()`` raise with the absolute path. Reads must stay
total (#1768): such a tier holds no blocks, its exclusion root is
``os.path.realpath`` of the spelled one (``resolve_path``), and compose still
drops a hit the index recorded under it. ``set`` and ``delete``
refuse a tier that fails to resolve with the path-free link refusal.

The 3.12 failure is injected by patching ``Path.resolve`` for the tier path,
so every Python sees it; the real-loop tests are a 3.12 check on CI's 3.12
shard and pin the same answers on 3.13+.
"""

from __future__ import annotations

import errno
from pathlib import Path
from types import SimpleNamespace

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.pinned import ContextAssembler, PinnedContextStore
from memtomem.search.pipeline import (
    RetrievalStats,
    _exclude_source_roots,
    _normalize_source_roots,
    _source_is_excluded,
)

_TIER_DIR = {"project_shared": "memories", "project_local": "memories.local"}
_SCOPES = tuple(_TIER_DIR)
_REFUSAL = r"is a link \(or cannot be inspected\).*nothing was changed"


def _store(tmp_path: Path) -> tuple[PinnedContextStore, Path]:
    config = Mem2MemConfig()
    config.indexing.memory_dirs = [tmp_path / "user-memory"]
    project = tmp_path / "project"
    (project / ".memtomem").mkdir(parents=True)
    store = PinnedContextStore(config, project_root=project)
    store.set("profile", "user profile", scope="user")
    return store, store.project_root  # type: ignore[return-value]


def _tier(root: Path, scope: str) -> Path:
    return root / ".memtomem" / _TIER_DIR[scope]


def _other(scope: str) -> str:
    return next(s for s in _SCOPES if s != scope)


#: How resolving the tier fails: Python 3.12's loop error, or an ``OSError``.
_FAILURES = ("loop", "oserror")


def _fail_resolving(monkeypatch, tier: Path, kind: str = "loop") -> None:
    """Make ``Path.resolve`` raise for ``tier`` and below, as a loop does on 3.12."""
    real_resolve = Path.resolve

    def _resolve(self, *args, **kwargs):
        if self == tier or self.is_relative_to(tier):
            if kind == "oserror":
                raise OSError(errno.ENAMETOOLONG, "File name too long", str(self))
            raise RuntimeError(f"Symlink loop from '{self}'")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)


def _loop(tier: Path) -> None:
    try:
        tier.symlink_to(tier, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows without symlinks
        pytest.skip(f"cannot create a symlink here: {exc}")


def _expected_roots(store: PinnedContextStore, root: Path, failed: str) -> tuple[Path, ...]:
    other = _other(failed)
    roots = {
        "user": (store.user_base / "pinned").resolve(),  # type: ignore[operator]
        other: (_tier(root, other) / "pinned").resolve(),
        failed: _tier(root, failed) / "pinned",
    }
    return tuple(roots[s] for s in ("user", "project_shared", "project_local"))


# -- the injected 3.12 failure, on every Python ---------------------------------------------


@pytest.mark.parametrize("kind", _FAILURES)
@pytest.mark.parametrize("scope", _SCOPES)
def test_exclusion_roots_give_the_spelled_root_of_a_tier_that_fails_to_resolve(
    tmp_path, monkeypatch, scope, kind
):
    store, root = _store(tmp_path)
    _fail_resolving(monkeypatch, _tier(root, scope), kind)
    assert store.search_exclusion_roots() == _expected_roots(store, root, scope)


@pytest.mark.parametrize("kind", _FAILURES)
@pytest.mark.parametrize("scope", _SCOPES)
def test_list_reads_the_other_tiers_when_one_fails_to_resolve(tmp_path, monkeypatch, scope, kind):
    store, root = _store(tmp_path)
    _fail_resolving(monkeypatch, _tier(root, scope), kind)
    assert [(b.block_id, b.scope, b.content) for b in store.list()] == [
        ("profile", "user", "user profile")
    ]


@pytest.mark.parametrize("kind", _FAILURES)
@pytest.mark.parametrize("scope", _SCOPES)
def test_get_from_a_tier_that_fails_to_resolve_finds_nothing(tmp_path, monkeypatch, scope, kind):
    store, root = _store(tmp_path)
    _fail_resolving(monkeypatch, _tier(root, scope), kind)
    assert store.get("profile", scope=scope) is None


def test_get_still_reports_an_invalid_id_before_the_tier(tmp_path, monkeypatch):
    store, root = _store(tmp_path)
    _fail_resolving(monkeypatch, _tier(root, "project_local"))
    with pytest.raises(ValueError, match="block id"):
        store.get("../escape", scope="project_local")


def _assert_path_free_refusal(exc: pytest.ExceptionInfo[ValueError], tmp_path: Path) -> None:
    assert str(tmp_path) not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__


@pytest.mark.parametrize("kind", _FAILURES)
@pytest.mark.parametrize("scope", _SCOPES)
def test_set_into_a_tier_that_fails_to_resolve_after_its_check_is_refused(
    tmp_path, monkeypatch, scope, kind
):
    store, root = _store(tmp_path)
    before = sorted(p for p in tmp_path.rglob("*") if p.is_file())
    # The race: every entry passed the link check, then the tier became a loop.
    # A context, not ``undo()``: the autouse isolation fixtures share ``monkeypatch``.
    with monkeypatch.context() as patch:
        patch.setattr("memtomem.pinned.entry_is_link", lambda _entry: False)
        _fail_resolving(patch, _tier(root, scope), kind)
        with pytest.raises(ValueError, match=_REFUSAL) as exc:
            store.set("team", "safe", scope=scope, confirm_project_shared=True)
    _assert_path_free_refusal(exc, tmp_path)
    assert f".memtomem/{_TIER_DIR[scope]}" in str(exc.value)
    assert sorted(p for p in tmp_path.rglob("*") if p.is_file()) == before


@pytest.mark.parametrize("kind", _FAILURES)
@pytest.mark.parametrize("scope", _SCOPES)
def test_delete_from_a_tier_that_fails_to_resolve_after_its_check_is_refused(
    tmp_path, monkeypatch, scope, kind
):
    store, root = _store(tmp_path)
    block = store.set("team", "safe", scope=scope, confirm_project_shared=True)
    saved = block.source_path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr("memtomem.pinned.entry_is_link", lambda _entry: False)
        _fail_resolving(patch, _tier(root, scope), kind)
        with pytest.raises(ValueError, match=_REFUSAL) as exc:
            store.delete("team", scope=scope, confirm_project_shared=True)
    _assert_path_free_refusal(exc, tmp_path)
    assert block.source_path.read_bytes() == saved


def test_a_user_tier_that_fails_to_resolve_still_raises(tmp_path, monkeypatch):
    """The catches are for project tiers; the user tier is the user's own layout."""
    store, _root = _store(tmp_path)
    _fail_resolving(monkeypatch, store.user_base)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        store.list()
    with pytest.raises(RuntimeError):
        store.search_exclusion_roots()
    with pytest.raises(RuntimeError):
        store.get("profile", scope="user")


# -- read errors below a tier that resolved --------------------------------------------------


def _fail_reading(monkeypatch, under: Path, exc: OSError) -> None:
    real_read_text = Path.read_text

    def _read_text(self, *args, **kwargs):
        if self.is_relative_to(under):
            raise exc
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)


def test_get_through_a_loop_finds_nothing(tmp_path, monkeypatch):
    """3.13+: resolve returns the spelled path through the loop, and the read hits ELOOP."""
    store, root = _store(tmp_path)
    tier = _tier(root, "project_local")
    _fail_reading(monkeypatch, tier, OSError(errno.ELOOP, "Too many levels of symbolic links"))
    assert store.get("profile", scope="project_local") is None


def test_get_answers_einval_as_a_loop_only_when_the_tier_is_a_link(tmp_path, monkeypatch):
    """Windows answers a loop with EINVAL, and gives EINVAL for other things too."""
    store, root = _store(tmp_path)
    tier = _tier(root, "project_local")
    _fail_reading(monkeypatch, tier, OSError(errno.EINVAL, "Invalid argument"))
    with pytest.raises(OSError):
        store.get("profile", scope="project_local")
    monkeypatch.setattr("memtomem.pinned.project_tier_is_relinked", lambda _tier: True)
    assert store.get("profile", scope="project_local") is None


def test_get_still_raises_other_read_errors(tmp_path, monkeypatch):
    store, root = _store(tmp_path)
    tier = _tier(root, "project_local")
    _fail_reading(monkeypatch, tier, PermissionError(errno.EACCES, "Permission denied"))
    with pytest.raises(PermissionError):
        store.get("profile", scope="project_local")


@pytest.mark.parametrize("scope", _SCOPES)
def test_list_skips_a_tier_it_cannot_enumerate(tmp_path, monkeypatch, scope):
    store, root = _store(tmp_path)
    store.set("team", "safe", scope=scope, confirm_project_shared=True)
    pinned = (_tier(root, scope) / "pinned").resolve()
    real_rglob = Path.rglob

    def _denied():
        # Raised while the walk is consumed, not when it is created.
        raise PermissionError(errno.EACCES, "Permission denied")
        yield  # pragma: no cover - makes this a generator

    def _rglob(self, *args, **kwargs):
        if self == pinned:
            return _denied()
        return real_rglob(self, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", _rglob)
    assert [(b.block_id, b.scope) for b in store.list()] == [("profile", "user")]


# -- a real loop, on whatever Python runs this ----------------------------------------------


@pytest.mark.requires_symlinks
def test_a_looping_tier_reads_as_empty(tmp_path):
    store, root = _store(tmp_path)
    _loop(_tier(root, "project_local"))
    assert store.search_exclusion_roots() == _expected_roots(store, root, "project_local")
    assert [(b.block_id, b.scope) for b in store.list()] == [("profile", "user")]
    assert store.get("profile", scope="project_local") is None


@pytest.mark.requires_symlinks
def test_the_pipeline_compares_against_a_looping_root_without_raising(tmp_path):
    _store_, root = _store(tmp_path)
    tier = _tier(root, "project_local")
    _loop(tier)
    spelled = tier / "pinned"
    assert _normalize_source_roots((spelled,)) == (spelled,)
    assert _source_is_excluded(spelled / "general" / "old.md", (spelled,))
    assert not _source_is_excluded(root / "notes.md", (spelled,))


@pytest.mark.requires_symlinks
def test_a_looping_tier_under_a_linked_dot_memtomem_still_excludes_its_indexed_hit(tmp_path):
    """The index recorded the canonical path; the root must keep that prefix.

    ``.memtomem -> actual``, and ``actual/memories.local`` becomes a loop after
    a block below it was indexed. Falling back to the spelled path would give
    a root under ``.memtomem/`` while the stored source is under ``actual/``.
    """
    store, root = _store(tmp_path)
    (root / ".memtomem").rmdir()
    actual = root / "actual"
    (actual / "memories.local" / "pinned" / "general").mkdir(parents=True)
    (root / ".memtomem").symlink_to(actual, target_is_directory=True)
    stored = (_tier(root, "project_local") / "pinned" / "general" / "old.md").resolve()
    (actual / "memories.local" / "pinned" / "general").rmdir()
    (actual / "memories.local" / "pinned").rmdir()
    (actual / "memories.local").rmdir()
    _loop(actual / "memories.local")

    roots = _normalize_source_roots(store.search_exclusion_roots())
    assert _source_is_excluded(stored, roots)
    assert not _source_is_excluded(root / "notes.md", roots)


@pytest.mark.parametrize("kind", _FAILURES)
def test_the_pipeline_falls_back_when_resolve_raises(tmp_path, monkeypatch, kind):
    """``realpath`` on the loop error, the spelled path on an ``OSError``; never a raise."""
    base = tmp_path.resolve()
    tier = base / "project" / ".memtomem" / "memories.local"
    spelled = tier / "pinned"
    other = base / "elsewhere"
    _fail_resolving(monkeypatch, tier, kind)
    assert _normalize_source_roots((spelled, other)) == tuple(sorted((spelled, other), key=str))
    assert _source_is_excluded(spelled / "general" / "old.md", (spelled,))
    assert not _source_is_excluded(other / "notes.md", (spelled,))


# -- queried compose with a hit filed under the looping tier --------------------------------


def _hit(chunk_id: str, source: Path, content: str) -> SimpleNamespace:
    chunk = SimpleNamespace(
        id=chunk_id,
        content=content,
        metadata=SimpleNamespace(source_file=source, namespace="default"),
    )
    return SimpleNamespace(chunk=chunk, score=0.5)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _FAILURES)
async def test_compose_drops_a_stale_hit_under_a_tier_that_fails_to_resolve(
    tmp_path, monkeypatch, kind
):
    store, root = _store(tmp_path)
    tier = _tier(root, "project_local")
    stale = _hit("stale", tier / "pinned" / "general" / "old.md", "stale pinned block")
    ordinary = _hit("ordinary", root / "notes.md", "ordinary memory")

    class Pipeline:
        async def search(self, **kwargs):
            roots = _normalize_source_roots(kwargs["exclude_source_roots"])
            return _exclude_source_roots([stale, ordinary], roots), RetrievalStats(
                score_scale="rrf"
            )

    _fail_resolving(monkeypatch, tier, kind)
    bundle = await ContextAssembler(store, Pipeline()).compose("deployment")
    assert [b.block_id for b in bundle.pinned] == ["profile"]
    assert [r["content"] for r in bundle.retrieved] == ["ordinary memory"]
