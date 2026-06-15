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

import os
import subprocess
from pathlib import Path

import pytest

from memtomem.wiki.commit import (
    ResolvedTarget,
    WikiTargetChangedError,
    commit_targets,
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
