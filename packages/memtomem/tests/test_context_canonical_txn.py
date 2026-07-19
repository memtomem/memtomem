"""ADR-0030 §6 (PR-B2a): canonical name-lock primitive + coverage pins.

Pins the new ``context/_canonical_txn.py`` primitives and the invariant they
enforce across every first-party canonical writer:

- name-keyed, **layout-independent** identity (flat ``<name>.md`` and dir
  ``<name>/`` share one lock; the root is resolved so differently-normalized
  callers still collide),
- resolve-INSIDE-the-lock (a flat→dir layout change while a Pull waits cannot
  strand a stale-path write),
- normative order canonical sidecar → versions.json (``versioning_op_locked``),
- the deterministic race pin (Codex M2): an ``Event`` fired *before* the real
  ``_file_lock`` acquire proves the competing writer actually reached the lock,
  so the test can never false-pass on scheduling.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from memtomem.context import _canonical_txn as txn
from memtomem.context._atomic import _file_lock, _lock_path_for
from memtomem.context._canonical_txn import (
    acquire_canonical_locks,
    canonical_lock_path,
    canonical_sidecar_lock,
    versioning_op_locked,
    write_canonical_locked,
)


# ── identity ────────────────────────────────────────────────────────────────


def test_lock_path_is_name_keyed_and_layout_independent(tmp_path: Path):
    root = tmp_path / ".memtomem" / "agents"
    root.mkdir(parents=True)
    # flat <root>/foo.md and dir <root>/foo/agent.md resolve to the SAME lock.
    assert canonical_lock_path(root, "foo") == root / ".foo.lock"
    # This is exactly what the skills importer already takes.
    assert canonical_lock_path(root, "foo") == _lock_path_for(root / "foo")


def test_lock_path_resolves_root_so_normalized_callers_collide(tmp_path: Path):
    root = tmp_path / ".memtomem" / "agents"
    root.mkdir(parents=True)
    # A non-normalized root (trailing ``..`` hop) must land on the same lock as
    # the resolved form — else a wiki writer and a Pull would not serialize.
    messy = root / "sub" / ".."
    assert canonical_lock_path(messy, "foo") == canonical_lock_path(root, "foo")


# ── write_canonical_locked ────────────────────────────────────────────────────


def _resolver(dst: Path, layout: str = "dir"):
    return lambda: (dst, layout)


def test_write_canonical_locked_created_overwritten_exists(tmp_path: Path):
    root = tmp_path / "agents"
    dst = root / "foo" / "agent.md"

    outcome, got_dst, layout = write_canonical_locked(
        root, "foo", b"v1", resolve_target=_resolver(dst), overwrite=False
    )
    assert (outcome, got_dst, layout) == ("created", dst, "dir")
    assert dst.read_bytes() == b"v1"

    # exists + not overwrite → no write.
    outcome, _, _ = write_canonical_locked(
        root, "foo", b"v2", resolve_target=_resolver(dst), overwrite=False
    )
    assert outcome == "exists"
    assert dst.read_bytes() == b"v1"

    # exists + overwrite → snapshot the pre-image into versions/ then replace
    # (B2b snapshot-first).
    outcome, _, _ = write_canonical_locked(
        root, "foo", b"v2", resolve_target=_resolver(dst), overwrite=True
    )
    assert outcome == "overwritten"
    assert dst.read_bytes() == b"v2"
    # The old bytes are preserved as v1 in the per-artifact version store.
    assert (dst.parent / "versions" / "v1.md").read_bytes() == b"v1"


def test_write_canonical_locked_resolves_inside_the_lock(tmp_path: Path):
    """The destination is resolved under the lock, so a layout flip that lands
    while the writer waits is observed (no stale-path write)."""
    root = tmp_path / "agents"
    flat = root / "foo.md"
    dir_dst = root / "foo" / "agent.md"
    root.mkdir(parents=True)

    calls: list[str] = []

    def _resolve():
        # First (and only) call happens INSIDE the lock; return dir layout to
        # prove the resolver — not a pre-lock snapshot — decides the path.
        calls.append("resolved")
        return dir_dst, "dir"

    outcome, got, layout = write_canonical_locked(
        root, "foo", b"x", resolve_target=_resolve, overwrite=False
    )
    assert calls == ["resolved"]
    assert (outcome, got) == ("created", dir_dst)
    assert not flat.exists() and dir_dst.read_bytes() == b"x"


# ── write_canonical_locked: B2b overwrite branches ────────────────────────────


def test_overwrite_identical_is_noop_no_snapshot(tmp_path: Path):
    """A byte-identical overwrite writes nothing and accrues no version (the
    version store has no GC, so an unchanged re-import must not spam vN.md)."""
    root = tmp_path / "agents"
    dst = root / "foo" / "agent.md"
    write_canonical_locked(root, "foo", b"same", resolve_target=_resolver(dst), overwrite=False)
    mtime_before = dst.stat().st_mtime_ns

    outcome, _, _ = write_canonical_locked(
        root, "foo", b"same", resolve_target=_resolver(dst), overwrite=True
    )
    assert outcome == "identical"
    assert dst.read_bytes() == b"same"
    assert not (dst.parent / "versions").exists()
    assert not (dst.parent / "versions.json").exists()
    assert dst.stat().st_mtime_ns == mtime_before  # no rewrite


def test_overwrite_snapshot_records_note(tmp_path: Path):
    """The snapshot_note is threaded into the version manifest."""
    from memtomem.context import versioning

    root = tmp_path / "agents"
    dst = root / "foo" / "agent.md"
    write_canonical_locked(root, "foo", b"v1", resolve_target=_resolver(dst), overwrite=False)
    write_canonical_locked(
        root,
        "foo",
        b"v2",
        resolve_target=_resolver(dst),
        overwrite=True,
        snapshot_note="pre-overwrite snapshot (import from claude)",
    )
    manifest = versioning.load_manifest(dst.parent)
    assert manifest.versions["v1"].note == "pre-overwrite snapshot (import from claude)"


def test_overwrite_flat_layout_is_refused(tmp_path: Path):
    """A flat-layout canonical has no versions/ store, so overwrite is refused
    (no write) rather than clobbered unsnapshotted."""
    root = tmp_path / "agents"
    flat = root / "foo.md"
    root.mkdir(parents=True)
    flat.write_bytes(b"orig")

    outcome, got_dst, layout = write_canonical_locked(
        root, "foo", b"new", resolve_target=_resolver(flat, layout="flat"), overwrite=True
    )
    assert (outcome, layout) == ("flat_refused", "flat")
    assert flat.read_bytes() == b"orig"  # untouched


def test_overwrite_snapshots_accrue_v1_v2(tmp_path: Path):
    """Successive differing overwrites snapshot each pre-image as v1, v2, …"""
    root = tmp_path / "agents"
    dst = root / "foo" / "agent.md"
    write_canonical_locked(root, "foo", b"v1", resolve_target=_resolver(dst), overwrite=False)
    write_canonical_locked(root, "foo", b"v2", resolve_target=_resolver(dst), overwrite=True)
    write_canonical_locked(root, "foo", b"v3", resolve_target=_resolver(dst), overwrite=True)

    assert dst.read_bytes() == b"v3"
    assert (dst.parent / "versions" / "v1.md").read_bytes() == b"v1"
    assert (dst.parent / "versions" / "v2.md").read_bytes() == b"v2"


def test_overwrite_snapshot_failure_is_fail_closed(tmp_path: Path):
    """If the snapshot can't be taken (a malformed versions.json), the write is
    aborted and the destination is left untouched — SnapshotError, not clobber."""
    root = tmp_path / "agents"
    dst = root / "foo" / "agent.md"
    write_canonical_locked(root, "foo", b"v1", resolve_target=_resolver(dst), overwrite=False)
    # Corrupt the manifest so create_version's load raises VersionError.
    (dst.parent / "versions.json").write_text("]not json[")

    with pytest.raises(txn.SnapshotError):
        write_canonical_locked(root, "foo", b"v2", resolve_target=_resolver(dst), overwrite=True)
    assert dst.read_bytes() == b"v1"  # untouched


def test_overwrite_snapshot_oserror_is_wrapped_fail_closed(tmp_path: Path, monkeypatch):
    """A raw OSError from the version-store write (disk full, EACCES) is wrapped
    as a durable SnapshotError — not left to abort the whole import — and the
    canonical is untouched (Codex M1)."""
    root = tmp_path / "agents"
    dst = root / "foo" / "agent.md"
    write_canonical_locked(root, "foo", b"v1", resolve_target=_resolver(dst), overwrite=False)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(txn.versioning, "create_version", boom)
    with pytest.raises(txn.SnapshotError):
        write_canonical_locked(root, "foo", b"v2", resolve_target=_resolver(dst), overwrite=True)
    assert dst.read_bytes() == b"v1"  # untouched (fail-closed)


def test_overwrite_child_lock_timeout_fails_closed(tmp_path: Path):
    """A foreign holder of the versions.json child lock surfaces as a
    TimeoutError (mapped by callers to lock_timeout), bounded by the budget —
    not a fresh child allowance — and the canonical is untouched."""
    root = tmp_path / "agents"
    dst = root / "foo" / "agent.md"
    write_canonical_locked(root, "foo", b"v1", resolve_target=_resolver(dst), overwrite=False)

    from memtomem.context import versioning

    child_lock = _lock_path_for(versioning.versions_json_path(dst.parent))
    with _file_lock(child_lock, timeout=None):
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            write_canonical_locked(
                root,
                "foo",
                b"v2",
                resolve_target=_resolver(dst),
                overwrite=True,
                lock_timeout=0.2,
            )
        elapsed = time.monotonic() - t0
    assert elapsed < 2.0  # bounded by the budget, not a fresh 30s
    assert dst.read_bytes() == b"v1"  # untouched (fail-closed)


def test_overwrite_snapshot_budget_is_shared_not_fresh(tmp_path: Path, monkeypatch):
    """The child versions.json lock receives the REMAINING budget after the
    canonical acquisition, not a fresh full allowance (Codex M3 — the weak
    elapsed-bound test could not tell the two apart). Hold the canonical lock in
    a background thread for a measured window so the overwrite must wait; the
    budget create_version sees must reflect that consumed time."""
    root = tmp_path / "agents"
    dst = root / "foo" / "agent.md"
    write_canonical_locked(root, "foo", b"v1", resolve_target=_resolver(dst), overwrite=False)

    captured: dict[str, float | None] = {}
    real_create = txn.versioning.create_version

    def spy(*a, **k):
        captured["lock_timeout"] = k.get("lock_timeout")
        return real_create(*a, **k)

    monkeypatch.setattr(txn.versioning, "create_version", spy)

    hold_s = 0.4
    budget_s = 5.0
    lock_path = canonical_lock_path(root, "foo")
    holding = threading.Event()

    def _hold():
        with _file_lock(lock_path, timeout=None):
            holding.set()
            time.sleep(hold_s)

    holder = threading.Thread(target=_hold)
    holder.start()
    assert holding.wait(2.0)  # the canonical lock is genuinely held

    write_canonical_locked(
        root,
        "foo",
        b"v2",
        resolve_target=_resolver(dst),
        overwrite=True,
        lock_timeout=budget_s,
    )
    holder.join(2.0)

    # The child got the shared remaining budget: strictly less than the full
    # ``budget_s`` by roughly the ``hold_s`` the canonical acquisition consumed.
    # A fresh-budget bug would forward the full ``budget_s`` unchanged.
    assert captured["lock_timeout"] is not None
    assert captured["lock_timeout"] < budget_s - (hold_s / 2)
    assert dst.read_bytes() == b"v2"
    assert (dst.parent / "versions" / "v1.md").read_bytes() == b"v1"


# ── acquire_canonical_locks ───────────────────────────────────────────────────


def test_acquire_canonical_locks_sorted_and_deduped(tmp_path: Path, monkeypatch):
    a = tmp_path / "a" / "agents"
    b = tmp_path / "b" / "agents"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    acquired: list[Path] = []
    real = _file_lock

    def _spy(lock_path, *, timeout=None):
        acquired.append(lock_path)
        return real(lock_path, timeout=timeout)

    monkeypatch.setattr(txn, "_file_lock", _spy)

    # Duplicate (b, foo) is deduped; the two distinct locks come out sorted.
    with acquire_canonical_locks([(b, "foo"), (a, "foo"), (b, "foo")]):
        pass

    want = sorted([canonical_lock_path(a, "foo"), canonical_lock_path(b, "foo")], key=str)
    assert acquired == want


def test_acquire_canonical_locks_shares_one_budget(tmp_path: Path, monkeypatch):
    a = tmp_path / "a" / "agents"
    b = tmp_path / "b" / "agents"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    seen: list[float | None] = []
    real = _file_lock

    def _slow(lock_path, *, timeout=None):
        seen.append(timeout)
        time.sleep(0.1)  # consume budget while "acquiring"
        return real(lock_path, timeout=timeout)

    monkeypatch.setattr(txn, "_file_lock", _slow)
    with acquire_canonical_locks([(a, "foo"), (b, "foo")], timeout=5.0):
        pass
    assert len(seen) == 2
    assert all(t is not None for t in seen)
    # Whole-call deadline: the 0.1s spent on the first comes off the second.
    assert seen[1] <= seen[0] - 0.05


# ── versioning_op_locked ordering ─────────────────────────────────────────────


def test_versioning_op_locked_takes_canonical_before_child(tmp_path: Path, monkeypatch):
    """Normative order: the ``<name>.lock`` sidecar precedes the child
    (versions.json) lock the op takes internally."""
    root = tmp_path / "agents"
    artifact_dir = root / "foo"
    artifact_dir.mkdir(parents=True)
    order: list[str] = []
    real = _file_lock

    def _rec(lock_path, *, timeout=None):
        order.append(lock_path.name)
        return real(lock_path, timeout=timeout)

    monkeypatch.setattr(txn, "_file_lock", _rec)

    def _op(lock_timeout):
        # Simulate the child lock a real versioning op takes internally — go
        # through the recorded ``txn._file_lock`` (the real create_version takes
        # its versions.json lock the same way, from its own module namespace).
        with txn._file_lock(_lock_path_for(artifact_dir / "versions.json"), timeout=lock_timeout):
            return "done"

    assert versioning_op_locked(artifact_dir, timeout=None, op=_op) == "done"
    # canonical sidecar (.foo.lock) recorded before the child (.versions.json.lock).
    assert order[0] == ".foo.lock"
    assert ".versions.json.lock" in order
    assert order.index(".foo.lock") < order.index(".versions.json.lock")


# ── deterministic race pin (Codex M2) ─────────────────────────────────────────


def test_write_blocks_while_canonical_lock_held_barrier(tmp_path: Path, monkeypatch):
    """Hold the canonical lock; a competing ``write_canonical_locked`` must
    block at acquire. An ``Event`` fired BEFORE the real acquire proves the
    worker actually reached the lock, so a favorable schedule can't false-pass.
    """
    root = tmp_path / "agents"
    dst = root / "foo" / "agent.md"
    root.mkdir(parents=True)

    reached = threading.Event()
    real = _file_lock

    def _barrier(lock_path, *, timeout=None):
        # Only the competing writer's acquire of the canonical lock signals.
        if lock_path == canonical_lock_path(root, "foo"):
            reached.set()
        return real(lock_path, timeout=timeout)

    result: dict[str, object] = {}

    def _worker():
        monkeypatch.setattr(txn, "_file_lock", _barrier)
        outcome, _, _ = write_canonical_locked(
            root, "foo", b"worker", resolve_target=_resolver(dst), overwrite=True
        )
        result["outcome"] = outcome

    with canonical_sidecar_lock(root, "foo"):
        # Pre-create so the worker's overwrite path is exercised.
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"pre")
        t = threading.Thread(target=_worker)
        t.start()
        # The worker reached the lock (proved) but must be blocked behind us:
        assert reached.wait(timeout=5.0), "worker never attempted the canonical lock"
        # Give it a beat; it must NOT have written while we hold the lock.
        time.sleep(0.2)
        assert dst.read_bytes() == b"pre"
        assert "outcome" not in result
    t.join(timeout=5.0)
    assert result["outcome"] == "overwritten"
    assert dst.read_bytes() == b"worker"


def test_write_canonical_locked_times_out_when_held(tmp_path: Path):
    """A held canonical lock makes a bounded acquire raise ``TimeoutError`` —
    the foundation every caller maps to a LOCK_TIMEOUT skip / HTTP 503."""
    root = tmp_path / "agents"
    dst = root / "foo" / "agent.md"
    root.mkdir(parents=True)

    held = threading.Event()
    release = threading.Event()

    def _holder():
        with canonical_sidecar_lock(root, "foo"):
            held.set()
            release.wait(timeout=5.0)

    t = threading.Thread(target=_holder)
    t.start()
    try:
        assert held.wait(timeout=5.0)
        with pytest.raises(TimeoutError):
            write_canonical_locked(
                root, "foo", b"x", resolve_target=_resolver(dst), overwrite=True, lock_timeout=0.2
            )
        assert not dst.exists()
    finally:
        release.set()
        t.join(timeout=5.0)


# ── skills delete acquires the lock unconditionally (Codex re-gate) ────────────


def test_skill_delete_acquires_canonical_lock_even_when_absent(tmp_path: Path, monkeypatch):
    """ADR-0030 §6: skill delete must acquire the canonical name lock
    UNCONDITIONALLY — a pre-lock ``skill_dir.exists()`` gate would skip the lock
    when the skill looks absent, so a concurrent creator/transfer holding the
    lock could materialize it while delete returns ``deleted: []`` without ever
    contending. Pin: the lock IS taken for an absent skill.
    """
    from memtomem.web.routes import context_skills as cs

    (tmp_path / ".memtomem" / "skills").mkdir(parents=True)
    acquired: list[str] = []
    real = cs.canonical_sidecar_lock

    def _rec(root: Path, name: str, *, timeout: float | None = None):
        acquired.append(name)
        return real(root, name, timeout=timeout)

    monkeypatch.setattr(cs, "canonical_sidecar_lock", _rec)

    result = asyncio.run(
        cs.delete_skill(
            "ghost",
            cascade=False,
            project_root=tmp_path,
            target_scope="project_shared",
            allow_host_writes=False,
        )
    )
    assert result == {"deleted": [], "skipped": []}
    assert acquired == ["ghost"]  # lock acquired despite the skill being absent
