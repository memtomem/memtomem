"""Tests for :mod:`memtomem.wiki.commit` — the shared isolated-commit engine.

``commit_targets`` is the single code path behind both the web Commit affordance
(ADR-0027 §3, ``web/routes/wiki_mutations.py``) and ``mm wiki ... commit``. These
exercise it directly: the cross-process lock-path determinism that makes the
web↔CLI exclusion real, the ``expected_head=None`` "commit onto current HEAD"
mode the CLI uses, commit isolation (never a bare ``git add .``), the no-op path,
the stale-token / TOCTOU guards, the ``expected_head`` CAS, and the race-guarded
``.bak`` cleanup (including the no-op path and the concurrent-fresh-backup case).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import portalocker
import pytest
from helpers import BUDGET_TOLERANCE_S

from memtomem._runtime_paths import runtime_dir
from memtomem.context import _atomic as _atomic_mod
from memtomem.context._atomic import _file_lock
from memtomem.wiki import commit as wiki_commit
from memtomem.wiki.commit import (
    ResolvedTarget,
    WikiLockUnavailableError,
    WikiTargetChangedError,
    commit_targets,
    legacy_wiki_commit_lock_path,
    wiki_commit_lock,
    wiki_commit_lock_path,
)
from memtomem.wiki.store import WikiHeadMovedError, WikiStore

# ``wiki_root`` / ``git_identity`` fixtures come from conftest.py (which imports
# them from _wiki_fixtures), so they need no import here.


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


def _committed_skill(root: Path, name: str = "demo", body: bytes = b"# canonical\n") -> WikiStore:
    store = WikiStore.at_default()
    store.init()
    d = root / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(body)
    _git(root, "add", ".")
    _git(root, "commit", "-m", f"add {name}")
    return store


def _target(store: WikiStore, rel: str, expected_mtime_ns: int | None = None) -> ResolvedTarget:
    return ResolvedTarget(rel=rel, path=store.root / rel, expected_mtime_ns=expected_mtime_ns)


class _ClockAfterFirstRead:
    """Stand-in for the ``time`` module that jumps after the deadline is set.

    ``wiki_commit_lock`` reads ``monotonic()`` twice: once to fix the deadline,
    once to compute what is left for the canonical leg. Returning *first* then
    *later* forces the ``remaining == 0.0`` branch with no wall-clock waiting.
    Only the name bound in ``wiki.commit`` is replaced, so ``_file_lock``'s own
    timing (a different module's import) keeps using the real clock.
    """

    def __init__(self, first: float, later: float) -> None:
        self._readings = [first]
        self._later = later

    def monotonic(self) -> float:
        return self._readings.pop(0) if self._readings else self._later


# ── lock path determinism (the web↔CLI mutual-exclusion contract) ──────────


def test_lock_path_is_deterministic_for_a_root(tmp_path: Path) -> None:
    # Web and CLI must derive the SAME path from the same root or they would not
    # exclude each other; the path is keyed by the resolved root.
    a = wiki_commit_lock_path(tmp_path / "wiki")
    b = wiki_commit_lock_path(tmp_path / "wiki")
    assert a == b
    assert a.suffix == ".lock"


def test_lock_path_differs_by_root(tmp_path: Path) -> None:
    assert wiki_commit_lock_path(tmp_path / "w1") != wiki_commit_lock_path(tmp_path / "w2")


def test_lock_path_is_outside_the_wiki_tree(tmp_path: Path) -> None:
    # Never under <wiki>/.git — _file_lock mkdir's the parent and would forge a
    # bogus .git/ if the wiki were removed.
    root = tmp_path / "wiki"
    assert root not in wiki_commit_lock_path(root).parents


def test_lock_path_honours_the_runtime_dir_override(tmp_path: Path) -> None:
    # #2225: the lock used to be a raw tempfile.gettempdir() leaf, so every
    # tmp_path wiki root stranded a never-unlinked file in the developer's real
    # temp. Routing it through the runtime dir puts it where conftest's autouse
    # isolation (and, in production, the 0o700-validated runtime dir) can hold it.
    lock = wiki_commit_lock_path(tmp_path / "wiki")
    assert lock.parent == runtime_dir()
    assert Path(tempfile.gettempdir()) / "memtomem" not in lock.parents


def test_legacy_lock_path_is_also_isolated_under_pytest(tmp_path: Path) -> None:
    # The transitional legacy lock must not reintroduce the #2225 leak: it is
    # still acquired (so an upgrade stays exclusive) but redirected under test.
    legacy = legacy_wiki_commit_lock_path(tmp_path / "wiki")
    assert runtime_dir() in legacy.parents
    assert Path(tempfile.gettempdir()) / "memtomem" not in legacy.parents


def test_legacy_and_canonical_locks_are_distinct_files(tmp_path: Path) -> None:
    # _file_lock contends with itself across two open file descriptions in one
    # process, so collapsing the pair onto one path would self-deadlock.
    root = tmp_path / "wiki"
    assert legacy_wiki_commit_lock_path(root) != wiki_commit_lock_path(root)


def test_wiki_commit_lock_excludes_a_holder_of_the_pre_2225_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-#2225 process still excludes a new one — against the *literal* old path.

    Deriving the "old" process's lock from ``legacy_wiki_commit_lock_path`` would
    prove nothing: under pytest that takes the redirected branch, so the test
    would agree with itself no matter what the production formula said. Here the
    path is rebuilt from the pre-#2225 source verbatim, and the redirect is
    disabled for the legacy leg only (patching the name *commit.py* holds, not
    the one ``ensure_runtime_dir`` reads) so the canonical leg stays isolated.
    ``tempfile.tempdir`` is redirected so this still writes nothing to real temp.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "fake-tmp"))
    monkeypatch.setattr(wiki_commit, "_test_runtime_dir_override", lambda: None)
    root = tmp_path / "wiki"
    root.mkdir()

    # The pre-#2225 formula, copied from the commit that introduced this fix.
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    old_path = Path(tempfile.gettempdir()) / "memtomem" / f"wiki-commit-{digest}.lock"
    assert old_path == legacy_wiki_commit_lock_path(root), "legacy leg drifted from the old path"

    with _file_lock(old_path, timeout=None):
        with pytest.raises(TimeoutError):
            with wiki_commit_lock(root, timeout=0.1):
                pytest.fail("acquired while a pre-#2225 process held the lock")


def test_wiki_commit_lock_budget_is_shared_not_doubled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pair must stay bounded below the web handler's ``asyncio.timeout(60)``;
    nesting two full-budget waits would have doubled it.

    Pinned on the timeout each leg is *handed*, under a fake clock that makes the
    legacy leg consume a known slice of the budget. Both halves matter (#2257):

    - Asserting elapsed wall time instead measured the runner as much as the
      lock, and flaked on loaded Windows CI at a ceiling deliberately set to the
      doubling being ruled out, so it could not be loosened without unpinning
      the bug.
    - Spying the timeouts alone would not distinguish either: with the legacy
      leg acquiring instantly the remaining budget still rounds to the whole
      budget, so a canonical leg handed the raw ``timeout`` looks identical to
      one handed the remainder. Only a legacy acquisition that visibly spends
      part of the deadline separates them.
    """
    root = tmp_path / "wiki"
    root.mkdir()
    budget = 30.0
    legacy_spend = 7.5
    legacy_path = legacy_wiki_commit_lock_path(root)
    seen: list[tuple[Path, float | None]] = []
    real_file_lock = wiki_commit._file_lock

    class _FakeClock:
        """Stands in for the ``time`` module inside ``wiki.commit``.

        ``monotonic`` is the module's only use of it (both reads of the shared
        deadline), so replacing the reference leaves nothing else stubbed.
        """

        def __init__(self, start: float) -> None:
            self.now = start

        def monotonic(self) -> float:
            return self.now

    clock = _FakeClock(1_000.0)

    @contextmanager
    def _spy(path: Path, *, timeout: float | None = None) -> Iterator[None]:
        seen.append((path, timeout))
        if path == legacy_path:
            # The legacy acquisition took this long; the canonical leg must be
            # handed what is left of the deadline, not a fresh budget.
            clock.now += legacy_spend
        with real_file_lock(path, timeout=timeout):
            yield

    monkeypatch.setattr(wiki_commit, "time", clock)
    monkeypatch.setattr(wiki_commit, "_file_lock", _spy)

    with wiki_commit_lock(root, timeout=budget):
        pass

    assert [path for path, _ in seen] == [legacy_path, wiki_commit_lock_path(root)]
    legacy_timeout, canonical_timeout = seen[0][1], seen[1][1]
    assert legacy_timeout == budget
    assert canonical_timeout is not None
    # The remainder, never a second full budget: the two waits together stay
    # within the caller's budget however the time is split between them.
    assert canonical_timeout == pytest.approx(budget - legacy_spend)
    assert legacy_spend + canonical_timeout <= budget + BUDGET_TOLERANCE_S


def test_exhausted_budget_still_attempts_the_canonical_leg_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # remaining == 0.0 must mean "one non-blocking attempt", not "skip" and not
    # "wait forever". A fake clock pins the branch deterministically rather than
    # racing a real deadline.
    root = tmp_path / "wiki"
    root.mkdir()
    monkeypatch.setattr(wiki_commit, "time", _ClockAfterFirstRead(0.0, 1_000.0))
    with wiki_commit_lock(root, timeout=0.4):
        pass  # uncontended: the single LOCK_NB attempt succeeds


def test_exhausted_budget_raises_rather_than_waiting_when_contended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "wiki"
    root.mkdir()
    with _file_lock(wiki_commit_lock_path(root), timeout=None):
        monkeypatch.setattr(wiki_commit, "time", _ClockAfterFirstRead(0.0, 1_000.0))
        with pytest.raises(TimeoutError):
            with wiki_commit_lock(root, timeout=0.4):
                pytest.fail("acquired on an exhausted budget")


# ── an unusable lock home is classified, never a raw OSError ───────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner/mode validation")
def test_unsafe_runtime_dir_becomes_a_classified_lock_error(tmp_path: Path) -> None:
    # Routing the lock through the runtime dir (#2225) made ensure_runtime_dir's
    # validation refusals reachable from a wiki commit for the first time. They
    # must not escape as a bare OSError to the CLI/web adapters.
    runtime_dir().mkdir(parents=True, exist_ok=True)
    runtime_dir().chmod(0o755)  # group/world bits — ensure_runtime_dir refuses
    root = tmp_path / "wiki"
    root.mkdir()
    with pytest.raises(WikiLockUnavailableError):
        with wiki_commit_lock(root, timeout=0.1):
            pytest.fail("acquired against an unsafe runtime dir")


def test_contention_is_not_reclassified_as_unavailable(tmp_path: Path) -> None:
    # TimeoutError is itself an OSError; the wrap must let it through with its
    # own meaning or "someone is committing, retry" becomes "your dir is broken".
    root = tmp_path / "wiki"
    root.mkdir()
    with _file_lock(wiki_commit_lock_path(root), timeout=None):
        with pytest.raises(TimeoutError):
            with wiki_commit_lock(root, timeout=0.1):
                pytest.fail("acquired while contended")


def test_lock_backend_failure_is_classified_not_reported_as_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lock *call* that fails for a non-contention reason (EIO here) used to be
    # polled to the deadline and reported as TimeoutError "held by another
    # process" (#2229) — i.e. as 503 busy, which a retrying client would spin on
    # forever. It is an establishment failure and must reach the same arm as an
    # unusable lock home.
    root = tmp_path / "wiki"
    root.mkdir()

    def _fail(*args: object, **kwargs: object) -> None:
        exc = portalocker.LockException("lock failed")
        exc.__cause__ = OSError(5, "input/output error")  # EIO
        raise exc

    monkeypatch.setattr(_atomic_mod.portalocker, "lock", _fail)

    with pytest.raises(WikiLockUnavailableError):
        with wiki_commit_lock(root, timeout=30.0):
            pytest.fail("acquired against a failing lock backend")


def test_caller_oserror_keeps_its_own_type(tmp_path: Path) -> None:
    # The yield sits outside the guard: a failed write *under* the lock is the
    # caller's error, not a lock-establishment failure.
    root = tmp_path / "wiki"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        with wiki_commit_lock(root, timeout=1.0):
            (tmp_path / "absent-dir" / "f").write_text("x", encoding="utf-8")


# ── expected_head=None (CLI) commits onto current HEAD, isolated ───────────


def test_commits_onto_current_head_with_none(wiki_root: Path) -> None:
    store = _committed_skill(wiki_root)
    head0 = store.current_commit()
    target = store.root / "skills/demo/overrides/claude.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"# override\n")

    outcome = commit_targets(
        store, [_target(store, "skills/demo/overrides/claude.md")], message="add override"
    )

    assert outcome.committed is True
    assert outcome.wiki_head != head0
    assert outcome.wiki_dirty is False
    blob = _git(wiki_root, "show", f"{outcome.wiki_head}:skills/demo/overrides/claude.md")
    assert blob == "# override\n"


def test_isolation_unrelated_staged_not_swept(wiki_root: Path) -> None:
    store = _committed_skill(wiki_root)
    # an unrelated change, staged in the REAL index
    (wiki_root / "skills/demo/SKILL.md").write_bytes(b"# canonical EDITED\n")
    _git(wiki_root, "add", "skills/demo/SKILL.md")
    # a separate override to commit in isolation
    ov = wiki_root / "skills/demo/overrides/claude.md"
    ov.parent.mkdir(parents=True)
    ov.write_bytes(b"# override\n")

    outcome = commit_targets(
        store, [_target(store, "skills/demo/overrides/claude.md")], message="iso"
    )

    # the commit contains ONLY the override, not the staged SKILL.md edit
    files = _git(wiki_root, "show", "--name-only", "--format=", outcome.wiki_head).split()
    assert files == ["skills/demo/overrides/claude.md"]
    # the staged SKILL.md edit survives uncommitted in the working tree
    assert b"EDITED" in (wiki_root / "skills/demo/SKILL.md").read_bytes()


def test_multi_target_single_commit(wiki_root: Path) -> None:
    store = _committed_skill(wiki_root)
    (wiki_root / "skills/demo/SKILL.md").write_bytes(b"# canonical v2\n")
    ov = wiki_root / "skills/demo/overrides/claude.md"
    ov.parent.mkdir(parents=True)
    ov.write_bytes(b"# override\n")

    outcome = commit_targets(
        store,
        [_target(store, "skills/demo/SKILL.md"), _target(store, "skills/demo/overrides/claude.md")],
        message="both",
    )

    # ONE new commit carrying BOTH files
    files = sorted(_git(wiki_root, "show", "--name-only", "--format=", outcome.wiki_head).split())
    assert files == ["skills/demo/SKILL.md", "skills/demo/overrides/claude.md"]
    # exactly one commit ahead of the seed (the add-demo commit)
    assert _git(wiki_root, "rev-list", "--count", "HEAD").strip() == "3"


# ── no-op, stale-token, TOCTOU, CAS guards ─────────────────────────────────


def test_noop_when_bytes_match_head(wiki_root: Path) -> None:
    store = _committed_skill(wiki_root)
    outcome = commit_targets(store, [_target(store, "skills/demo/SKILL.md")], message="noop")
    assert outcome.committed is False
    assert outcome.wiki_dirty is False


def test_stale_token_raises_without_force(wiki_root: Path) -> None:
    store = _committed_skill(wiki_root)
    target_path = wiki_root / "skills/demo/SKILL.md"
    target_path.write_bytes(b"# edited\n")
    stale = target_path.stat().st_mtime_ns - 1  # a token that won't match disk
    with pytest.raises(WikiTargetChangedError):
        commit_targets(
            store, [_target(store, "skills/demo/SKILL.md", expected_mtime_ns=stale)], message="x"
        )


def test_stale_token_committed_with_force(wiki_root: Path) -> None:
    store = _committed_skill(wiki_root)
    target_path = wiki_root / "skills/demo/SKILL.md"
    target_path.write_bytes(b"# edited\n")
    stale = target_path.stat().st_mtime_ns - 1
    outcome = commit_targets(
        store,
        [_target(store, "skills/demo/SKILL.md", expected_mtime_ns=stale)],
        message="forced",
        force=True,
    )
    assert outcome.committed is True


def test_missing_target_raises(wiki_root: Path) -> None:
    store = _committed_skill(wiki_root)
    with pytest.raises(WikiTargetChangedError) as ei:
        commit_targets(store, [_target(store, "skills/demo/overrides/nope.md")], message="x")
    assert ei.value.current_mtime_ns == 0


def test_stale_expected_head_raises(wiki_root: Path) -> None:
    store = _committed_skill(wiki_root)
    stale_head = store.current_commit()
    # advance HEAD out of band so the passed expected_head is stale
    (wiki_root / "skills/demo/SKILL.md").write_bytes(b"# v2\n")
    _git(wiki_root, "commit", "-am", "v2")
    ov = wiki_root / "skills/demo/overrides/claude.md"
    ov.parent.mkdir(parents=True)
    ov.write_bytes(b"# ov\n")
    with pytest.raises(WikiHeadMovedError):
        commit_targets(
            store,
            [_target(store, "skills/demo/overrides/claude.md")],
            message="x",
            expected_head=stale_head,
        )


# ── race-guarded .bak cleanup ──────────────────────────────────────────────


def test_bak_cleaned_after_commit(wiki_root: Path) -> None:
    store = _committed_skill(wiki_root)
    ov = wiki_root / "skills/demo/overrides/claude.md"
    ov.parent.mkdir(parents=True)
    ov.write_bytes(b"# override\n")
    bak = ov.with_suffix(ov.suffix + ".bak")
    bak.write_bytes(b"# old\n")

    outcome = commit_targets(
        store, [_target(store, "skills/demo/overrides/claude.md")], message="x"
    )

    assert outcome.committed is True
    assert not bak.exists()  # the asset's own .bak was cleaned
    assert outcome.wiki_dirty is False


def test_bak_cleaned_on_noop_path(wiki_root: Path) -> None:
    store = _committed_skill(wiki_root)
    # SKILL.md == HEAD (no-op), but a stray .bak would keep the tree dirty
    bak = wiki_root / "skills/demo/SKILL.md.bak"
    bak.write_bytes(b"# old\n")
    outcome = commit_targets(store, [_target(store, "skills/demo/SKILL.md")], message="noop")
    assert outcome.committed is False
    assert not bak.exists()
    assert outcome.wiki_dirty is False


def test_concurrent_fresh_bak_preserved(wiki_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _committed_skill(wiki_root)
    ov = wiki_root / "skills/demo/overrides/claude.md"
    ov.parent.mkdir(parents=True)
    ov.write_bytes(b"# override\n")
    bak = ov.with_suffix(ov.suffix + ".bak")
    bak.write_bytes(b"# at-commit\n")  # snapshotted pre-commit

    real = WikiStore.commit_paths

    def _commit_then_fresh_bak(self, files, *, message, expected_head):  # noqa: ANN001
        sha = real(self, files, message=message, expected_head=expected_head)
        # a concurrent Save drops a FRESH .bak (distinct bytes + mtime) after the
        # snapshot, before cleanup — cleanup must skip it.
        bak.write_bytes(b"# fresh-from-concurrent-save\n")
        os.utime(bak, ns=(0, 0))
        return sha

    monkeypatch.setattr(WikiStore, "commit_paths", _commit_then_fresh_bak)
    commit_targets(store, [_target(store, "skills/demo/overrides/claude.md")], message="x")

    assert bak.exists()  # the fresh backup was NOT deleted
    assert bak.read_bytes() == b"# fresh-from-concurrent-save\n"
