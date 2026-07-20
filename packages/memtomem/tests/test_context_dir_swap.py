"""Tests for memtomem.context._dir_swap — the ADR-0030 §10 directory swap.

The module ships with NO production caller (PR-G4b wires the first one), so
everything here drives it directly. That is the same posture PR-G3 used for
``versioning.create_tree_version``: the storage/transaction layer is proven in
isolation, and a separate pin asserts the feature it unlocks is still refused.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from memtomem.context import _dir_swap
from memtomem.context._dir_swap import (
    SwapForeignDestination,
    SwapRecoveryError,
    marker_owns_staging,
    new_swap_suffix,
    recover_pending_swaps,
    staging_path_for,
    swap_dir_tree,
)

# Capability flags, computed once at module scope. NOT stacked ``skipif``
# decorators: a decorator's argument is evaluated when the class body executes,
# so an inner ``hasattr``-dependent call still runs even behind an outer
# platform guard that would have skipped it.
_HAS_MKFIFO = hasattr(os, "mkfifo")
_HAS_SIGALRM = hasattr(signal, "SIGALRM")
_requires_fifo = pytest.mark.skipif(not _HAS_MKFIFO, reason="mkfifo is POSIX-only")

SUFFIX = "999999-abc123"


def _mkdir_tree(path: Path, content: str) -> Path:
    """A minimal artifact tree whose bytes identify which copy survived."""
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(content, encoding="utf-8")
    (path / "nested").mkdir()
    (path / "nested" / "note.md").write_text(f"{content}-nested", encoding="utf-8")
    return path


def _read_tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root).as_posix()): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _paths(root: Path, name: str = "skill", suffix: str = SUFFIX) -> dict[str, Path]:
    return {
        "dst": root / name,
        "old": root / f".old-{name}-{suffix}.tmp",
        "staging": root / f".staging-{name}-{suffix}.tmp",
        "marker": root / f".swap-{name}-{suffix}.json",
    }


def _write_marker(
    root: Path,
    name: str = "skill",
    suffix: str = SUFFIX,
    overrides: dict[str, object] | None = None,
) -> Path:
    """Write a marker whose BASENAME is ``(name, suffix)`` and whose payload is
    the derived one, optionally corrupted by *overrides*.

    Basename and payload are separate arguments on purpose: the relational
    checks exist precisely to catch a payload that disagrees with the basename,
    so a helper that changed both at once could never exercise them.
    """
    p = _paths(root, name, suffix)
    payload: dict[str, object] = {
        "version": 1,
        "name": name,
        "suffix": suffix,
        "dst": p["dst"].name,
        "old": p["old"].name,
        "staging": p["staging"].name,
        "created_at": "2026-07-20T00:00:00Z",
    }
    payload.update(overrides or {})
    p["marker"].write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return p["marker"]


def _residue(root: Path) -> list[str]:
    """Every internal transient left under *root* — the "converged" assertion."""
    return sorted(
        e.name for e in root.iterdir() if e.name.startswith((".swap-", ".staging-", ".old-"))
    )


@contextmanager
def _deadline(seconds: int = 5) -> Iterator[None]:
    """Fail (rather than hang the suite) if the body blocks.

    The FIFO cases exist precisely because a blocking ``open``/``read`` would
    wedge every writer for the artifact while C0 is held. ``pytest.raises``
    alone cannot tell "refused" from "still waiting", so the refusal is
    asserted under a hard deadline.
    """
    if not _HAS_SIGALRM:  # pragma: no cover - POSIX-only tests are skipped
        yield
        return

    def _fire(signum: int, frame: object) -> None:
        raise TimeoutError("blocked past the deadline")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


class TestSwapNames:
    def test_suffix_shape(self) -> None:
        suffix = new_swap_suffix()
        assert _dir_swap._SUFFIX_RE.match(suffix)
        assert suffix.startswith(f"{os.getpid()}-")

    def test_staging_path_round_trips_through_the_swap(self, tmp_path: Path) -> None:
        """The grammar and its parser live together, so a path built by
        ``staging_path_for`` is always one ``swap_dir_tree`` can bind a marker to."""
        dst = tmp_path / "skill"
        suffix = new_swap_suffix()
        staging = staging_path_for(dst, suffix)
        _mkdir_tree(dst, "old")
        _mkdir_tree(staging, "new")

        swap_dir_tree(staging, dst)
        assert _read_tree(dst)["SKILL.md"] == "new"

    def test_staging_path_rejects_a_malformed_suffix(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            staging_path_for(tmp_path / "skill", "not-a-suffix")

    def test_refuses_a_foreign_staging_basename(self, tmp_path: Path) -> None:
        dst = _mkdir_tree(tmp_path / "skill", "old")
        staging = _mkdir_tree(tmp_path / ".staging-other-999999-abc123.tmp", "new")
        with pytest.raises(ValueError):
            swap_dir_tree(staging, dst)
        assert _read_tree(dst)["SKILL.md"] == "old"
        assert _residue(tmp_path) == [staging.name]

    def test_refuses_a_cross_parent_staging(self, tmp_path: Path) -> None:
        dst = _mkdir_tree(tmp_path / "a" / "skill", "old")
        staging = _mkdir_tree(tmp_path / "b" / f".staging-skill-{SUFFIX}.tmp", "new")
        with pytest.raises(ValueError):
            swap_dir_tree(staging, dst)
        assert _read_tree(dst)["SKILL.md"] == "old"

    def test_refuses_a_staging_that_is_not_a_directory(self, tmp_path: Path) -> None:
        dst = _mkdir_tree(tmp_path / "skill", "old")
        staging = tmp_path / f".staging-skill-{SUFFIX}.tmp"
        staging.write_text("not a tree", encoding="utf-8")
        with pytest.raises(ValueError):
            swap_dir_tree(staging, dst)
        assert _read_tree(dst)["SKILL.md"] == "old"


class TestMarkerPayload:
    def test_payload_is_basenames_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A marker that is copied or moved must not be able to name a tree
        outside its own directory, so no field may carry an absolute path."""
        dst = _mkdir_tree(tmp_path / "skill", "old")
        staging = _mkdir_tree(staging_path_for(dst, SUFFIX), "new")
        captured: dict[str, object] = {}
        marker = _paths(tmp_path)["marker"]

        real_write = _dir_swap.atomic_write_bytes

        def spy(path: Path, data: bytes, *args: object, **kwargs: object) -> None:
            if path == marker:
                captured.update(json.loads(data.decode()))
                captured["_full_fsync"] = kwargs.get("full_fsync")
            real_write(path, data, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(_dir_swap, "atomic_write_bytes", spy)
        swap_dir_tree(staging, dst)

        assert captured["version"] == 1
        assert captured["name"] == "skill"
        assert captured["suffix"] == SUFFIX
        assert captured["dst"] == "skill"
        assert captured["old"] == f".old-skill-{SUFFIX}.tmp"
        assert captured["staging"] == f".staging-skill-{SUFFIX}.tmp"
        assert captured["_full_fsync"] is True
        assert not any(str(v).startswith("/") for v in captured.values() if isinstance(v, str))

    def test_marker_is_durable_before_the_first_rename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Marker-then-rename, never the other way round: a power cut must not
        be able to make the renames visible with no record of the intent."""
        dst = _mkdir_tree(tmp_path / "skill", "old")
        staging = _mkdir_tree(staging_path_for(dst, SUFFIX), "new")
        marker = _paths(tmp_path)["marker"]
        events: list[str] = []

        real_write = _dir_swap.atomic_write_bytes
        real_rename = _dir_swap.rename_no_replace

        def write_spy(path: Path, data: bytes, *args: object, **kwargs: object) -> None:
            if path == marker:
                events.append("marker")
            real_write(path, data, *args, **kwargs)  # type: ignore[arg-type]

        def rename_spy(src: Path, target: Path) -> None:
            events.append("rename")
            real_rename(src, target)

        monkeypatch.setattr(_dir_swap, "atomic_write_bytes", write_spy)
        monkeypatch.setattr(_dir_swap, "rename_no_replace", rename_spy)
        swap_dir_tree(staging, dst)
        assert events == ["marker", "rename", "rename"]


class TestMarkerRead:
    def test_short_reads_are_looped_to_eof(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single ``os.read`` may return short: without the loop the marker
        would parse as truncated JSON and fail closed on a healthy artifact."""
        _mkdir_tree(tmp_path / "skill", "canonical")
        _write_marker(tmp_path)
        real_read = os.read
        monkeypatch.setattr(os, "read", lambda fd, n: real_read(fd, 1))

        assert recover_pending_swaps(tmp_path, "skill") is True  # row 7
        assert _residue(tmp_path) == []

    def test_oversized_marker_is_refused(self, tmp_path: Path) -> None:
        _mkdir_tree(tmp_path / "skill", "canonical")
        marker = _paths(tmp_path)["marker"]
        marker.write_bytes(b"x" * (_dir_swap._MARKER_MAX_BYTES + 1))

        with pytest.raises(SwapRecoveryError):
            recover_pending_swaps(tmp_path, "skill")
        assert marker.exists()

    @pytest.mark.requires_symlinks
    def test_symlinked_marker_is_refused(self, tmp_path: Path) -> None:
        _mkdir_tree(tmp_path / "skill", "canonical")
        real = tmp_path / "elsewhere.json"
        real.write_text("{}", encoding="utf-8")
        _paths(tmp_path)["marker"].symlink_to(real)

        with pytest.raises(SwapRecoveryError):
            recover_pending_swaps(tmp_path, "skill")
        assert real.exists()

    @_requires_fifo
    def test_fifo_marker_is_refused_without_blocking(self, tmp_path: Path) -> None:
        """The reason ``O_NONBLOCK`` + ``fstat`` on the descriptor exist: a
        correctly-named FIFO would otherwise block ``read`` forever while the
        canonical name lock is held, wedging every writer for this artifact."""
        _mkdir_tree(tmp_path / "skill", "canonical")
        os.mkfifo(_paths(tmp_path)["marker"])

        with _deadline(5):
            with pytest.raises(SwapRecoveryError):
                recover_pending_swaps(tmp_path, "skill")


class TestMarkerRelationalBinding:
    """Shape validation is not enough — a tampered marker must never be able to
    direct a later ``rmtree`` at an unrelated tree. Every case here must fail
    closed with NOTHING deleted."""

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"name": "other"}, id="name-mismatch"),
            pytest.param({"suffix": "111111-ffffff"}, id="suffix-mismatch"),
            pytest.param({"dst": "other"}, id="dst-mismatch"),
            pytest.param({"old": ".old-other-999999-abc123.tmp"}, id="old-mismatch"),
            pytest.param({"staging": "../escape"}, id="staging-separator"),
            pytest.param({"dst": ".."}, id="dst-dotdot"),
            pytest.param({"version": 2}, id="version-newer"),
            pytest.param({"version": None}, id="version-missing"),
        ],
    )
    def test_mismatched_field_fails_closed(
        self, tmp_path: Path, overrides: dict[str, object]
    ) -> None:
        p = _paths(tmp_path)
        _mkdir_tree(p["dst"], "canonical")
        _mkdir_tree(p["old"], "original")
        _write_marker(tmp_path, overrides=overrides)

        with pytest.raises(SwapRecoveryError):
            recover_pending_swaps(tmp_path, "skill")
        assert _read_tree(p["dst"])["SKILL.md"] == "canonical"
        assert _read_tree(p["old"])["SKILL.md"] == "original"
        assert p["marker"].exists()

    def test_truncated_json_fails_closed(self, tmp_path: Path) -> None:
        p = _paths(tmp_path)
        _mkdir_tree(p["dst"], "canonical")
        _mkdir_tree(p["old"], "original")
        p["marker"].write_text('{"version": 1, "name": "ski', encoding="utf-8")

        with pytest.raises(SwapRecoveryError):
            recover_pending_swaps(tmp_path, "skill")
        assert p["old"].is_dir()

    def test_two_markers_for_one_name_fail_closed(self, tmp_path: Path) -> None:
        """They describe two transactions over the same artifact, so acting on
        either could rename or delete a tree the other one owns."""
        p = _paths(tmp_path)
        _mkdir_tree(p["dst"], "canonical")
        _mkdir_tree(p["old"], "original")
        _write_marker(tmp_path)
        _write_marker(tmp_path, suffix="111111-ffffff")

        with pytest.raises(SwapRecoveryError):
            recover_pending_swaps(tmp_path, "skill")
        assert p["old"].is_dir()
        assert len([n for n in _residue(tmp_path) if n.startswith(".swap-")]) == 2

    def test_a_neighbouring_artifacts_marker_is_not_ours(self, tmp_path: Path) -> None:
        """``.swap-foo-*`` also prefix-matches ``.swap-foo-bar-<pid>-<rand>.json``,
        which belongs to the valid artifact ``foo-bar`` — the cross-destination
        shape #1871 fixed for the reaper."""
        _mkdir_tree(tmp_path / "foo", "canonical")
        neighbour = _write_marker(tmp_path, name="foo-bar")

        assert recover_pending_swaps(tmp_path, "foo") is False
        assert neighbour.exists()

    def test_a_non_conforming_swap_name_is_left_alone(self, tmp_path: Path) -> None:
        _mkdir_tree(tmp_path / "skill", "canonical")
        stray = tmp_path / ".swap-skill-notes.json"
        stray.write_text("{}", encoding="utf-8")

        assert recover_pending_swaps(tmp_path, "skill") is False
        assert stray.exists()


class TestSwapForward:
    def test_replaces_the_tree_and_leaves_no_residue(self, tmp_path: Path) -> None:
        dst = _mkdir_tree(tmp_path / "skill", "old")
        staging = _mkdir_tree(staging_path_for(dst, SUFFIX), "new")

        swap_dir_tree(staging, dst)

        assert _read_tree(dst) == {"SKILL.md": "new", "nested/note.md": "new-nested"}
        assert _residue(tmp_path) == []

    def test_post_commit_cleanup_failure_still_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rename 2 is the commit point. Reporting a cleanup failure as a write
        failure would tell the caller a write that actually landed had failed."""
        dst = _mkdir_tree(tmp_path / "skill", "old")
        staging = _mkdir_tree(staging_path_for(dst, SUFFIX), "new")

        def boom(path: Path) -> None:
            raise OSError(errno.EACCES, "nope")

        monkeypatch.setattr(_dir_swap.shutil, "rmtree", boom)
        swap_dir_tree(staging, dst)

        assert _read_tree(dst)["SKILL.md"] == "new"
        assert not _paths(tmp_path)["marker"].exists()

    def test_fsync_dir_unsupported_does_not_fail_the_swap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Durability degrades to process-crash consistency on filesystems that
        reject directory fsync; it never turns a correct swap into a failure."""
        dst = _mkdir_tree(tmp_path / "skill", "old")
        staging = _mkdir_tree(staging_path_for(dst, SUFFIX), "new")
        monkeypatch.setattr(_dir_swap, "fsync_dir", lambda p: False)

        swap_dir_tree(staging, dst)
        assert _read_tree(dst)["SKILL.md"] == "new"


class TestSwapUnwind:
    """Every row of the §4 forward-failure unwind table. A failure at a KNOWN
    point is not a crash and must not be left for the recovery machine."""

    def test_marker_write_failure_leaves_nothing_marked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dst = _mkdir_tree(tmp_path / "skill", "old")
        staging = _mkdir_tree(staging_path_for(dst, SUFFIX), "new")

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError(errno.EIO, "marker write failed")

        monkeypatch.setattr(_dir_swap, "atomic_write_bytes", boom)
        with pytest.raises(OSError):
            swap_dir_tree(staging, dst)

        assert _read_tree(dst)["SKILL.md"] == "old"
        # No marker AND no leftover staging: the write is atomic, so a failure
        # means nothing claims that tree, and leaving it would collide with the
        # suffix a retry allocates.
        assert _residue(tmp_path) == []

    def test_rename1_failure_restores_a_clean_slate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dst = _mkdir_tree(tmp_path / "skill", "old")
        staging = _mkdir_tree(staging_path_for(dst, SUFFIX), "new")

        def boom(src: Path, target: Path) -> None:
            raise OSError(errno.EIO, "rename 1 failed")

        monkeypatch.setattr(_dir_swap, "rename_no_replace", boom)
        with pytest.raises(OSError) as exc:
            swap_dir_tree(staging, dst)

        assert exc.value.errno == errno.EIO
        assert _read_tree(dst)["SKILL.md"] == "old"
        assert _residue(tmp_path) == []

    def test_rename1_hits_a_foreign_old_and_unwinds(self, tmp_path: Path) -> None:
        """The exclusive rename 1 refuses to adopt a pre-existing ``old`` — the
        state ``os.replace`` would have silently swallowed."""
        p = _paths(tmp_path)
        dst = _mkdir_tree(p["dst"], "old")
        staging = _mkdir_tree(p["staging"], "new")
        _mkdir_tree(p["old"], "foreign")

        with pytest.raises(OSError):
            swap_dir_tree(staging, dst)

        assert _read_tree(dst)["SKILL.md"] == "old"
        assert _read_tree(p["old"])["SKILL.md"] == "foreign"
        assert not p["marker"].exists()
        assert not p["staging"].exists()

    def test_rename2_failure_restores_dst_byte_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = _paths(tmp_path)
        dst = _mkdir_tree(p["dst"], "old")
        staging = _mkdir_tree(p["staging"], "new")
        before = _read_tree(dst)
        real_rename = _dir_swap.rename_no_replace

        def fail_promote(src: Path, target: Path) -> None:
            if src == p["staging"]:
                raise OSError(errno.EIO, "promote failed")
            real_rename(src, target)

        monkeypatch.setattr(_dir_swap, "rename_no_replace", fail_promote)
        with pytest.raises(OSError) as exc:
            swap_dir_tree(staging, dst)

        assert exc.value.errno == errno.EIO
        assert _read_tree(dst) == before
        assert _residue(tmp_path) == []


class TestUnwindRetainsWhatItCannotProve:
    """Three ways an unwind could destroy the tree it exists to protect. Each
    was found by review, and each turns on the same rule: an unwind may only
    delete what it can PROVE is no longer owned."""

    def test_a_failed_marker_unlink_retains_the_transients(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deleting the staging tree while the marker survives silently
        reclassifies the state: the fail-closed all-three row becomes the
        ``dst`` + ``old`` row, whose recovery deletes ``old``."""
        p = _paths(tmp_path)
        dst = _mkdir_tree(p["dst"], "canonical")
        staging = _mkdir_tree(p["staging"], "candidate")
        _mkdir_tree(p["old"], "foreign")  # makes the exclusive rename 1 fail
        real_unlink = Path.unlink

        def refuse_marker_unlink(self: Path, *args: object, **kwargs: object) -> None:
            if self == p["marker"]:
                raise OSError(errno.EACCES, "marker unlink refused")
            real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "unlink", refuse_marker_unlink)
        with pytest.raises(OSError):
            swap_dir_tree(staging, dst)

        assert p["marker"].is_file()
        assert _read_tree(p["staging"])["SKILL.md"] == "candidate"
        assert _read_tree(p["old"])["SKILL.md"] == "foreign"
        with pytest.raises(SwapForeignDestination):
            recover_pending_swaps(tmp_path, "skill")
        assert _read_tree(p["old"])["SKILL.md"] == "foreign"

    def test_cancellation_after_a_successful_rename_does_not_unwind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A signal can be delivered AFTER the kernel completed the rename and
        before the ``try`` block exits. Unwinding then would undo a move that
        actually happened, unlink the marker, and leave the original as
        unmarked ``.old-*`` debris the reaper is free to delete."""
        p = _paths(tmp_path)
        dst = _mkdir_tree(p["dst"], "original")
        staging = _mkdir_tree(p["staging"], "candidate")
        real_rename = _dir_swap.rename_no_replace

        def interrupt_after_rename1(src: Path, target: Path) -> None:
            real_rename(src, target)
            if target == p["old"]:
                raise KeyboardInterrupt

        monkeypatch.setattr(_dir_swap, "rename_no_replace", interrupt_after_rename1)
        with pytest.raises(KeyboardInterrupt):
            swap_dir_tree(staging, dst)

        assert p["marker"].is_file()
        assert _read_tree(p["old"])["SKILL.md"] == "original"

        # …and the state it left is exactly recovery row 2, which converges.
        assert recover_pending_swaps(tmp_path, "skill") is True
        assert _read_tree(p["dst"])["SKILL.md"] == "candidate"
        assert _residue(tmp_path) == []

    @pytest.mark.parametrize("row", ["row2", "row3"])
    def test_a_failed_marker_unlink_refuses_during_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row: str
    ) -> None:
        """Recovery is the prelude every canonical writer trusts, so a normal
        return means "resolved". If the marker survives it is not: the next
        write materializes ``dst``, and the run after that classifies a state
        the stale marker no longer describes. It refuses instead — and ``old``,
        the pre-image, is untouched."""
        p = _paths(tmp_path)
        _mkdir_tree(p["old"], "original")
        if row == "row2":
            _mkdir_tree(p["staging"], "candidate")
        else:
            _mkdir_tree(p["dst"], "promoted")
        _write_marker(tmp_path)
        real_unlink = Path.unlink

        def refuse_marker_unlink(self: Path, *args: object, **kwargs: object) -> None:
            if self == p["marker"]:
                raise OSError(errno.EACCES, "marker unlink refused")
            real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "unlink", refuse_marker_unlink)
        with pytest.raises(SwapRecoveryError):
            recover_pending_swaps(tmp_path, "skill")

        assert p["marker"].is_file()
        assert _read_tree(p["old"])["SKILL.md"] == "original"

    def test_a_pending_marker_refuses_a_second_swap(self, tmp_path: Path) -> None:
        """The marker write REPLACES, so starting a swap over an unresolved one
        would erase the record of it and let this transaction unwind over the
        other's transients. Recovery is a precondition, not a race."""
        p = _paths(tmp_path)
        dst = _mkdir_tree(p["dst"], "canonical")
        staging = _mkdir_tree(p["staging"], "candidate")
        pending = _write_marker(tmp_path, suffix="111111-ffffff")
        stranded = _mkdir_tree(tmp_path / ".old-skill-111111-ffffff.tmp", "original")

        with pytest.raises(ValueError):
            swap_dir_tree(staging, dst)

        assert pending.is_file()
        assert _read_tree(stranded)["SKILL.md"] == "original"
        assert _read_tree(dst)["SKILL.md"] == "canonical"
        assert not p["marker"].exists()


class TestSwapNestedUnwindRow4:
    """The nested failure that matters: the restore is refused because ``dst``
    came back. All three artifacts must survive — deleting the staging tree
    would turn this fail-closed row 4 into row 3, whose recovery deletes the
    original."""

    @staticmethod
    def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SwapRecoveryError:
        p = _paths(tmp_path)
        dst = _mkdir_tree(p["dst"], "old")
        staging = _mkdir_tree(p["staging"], "new")
        real_rename = _dir_swap.rename_no_replace

        def fail_promote_then_recreate(src: Path, target: Path) -> None:
            if src == p["staging"]:
                # A non-gateway writer materializes the destination during the
                # window, so the exclusive restore below fails EEXIST.
                _mkdir_tree(p["dst"], "foreign")
                raise OSError(errno.EIO, "promote failed")
            real_rename(src, target)

        monkeypatch.setattr(_dir_swap, "rename_no_replace", fail_promote_then_recreate)
        with pytest.raises(SwapRecoveryError) as exc:
            swap_dir_tree(staging, dst)
        return exc.value

    def test_all_three_artifacts_survive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._run(tmp_path, monkeypatch)
        p = _paths(tmp_path)
        assert p["marker"].is_file()
        assert _read_tree(p["old"])["SKILL.md"] == "old"
        assert _read_tree(p["staging"])["SKILL.md"] == "new"
        assert _read_tree(p["dst"])["SKILL.md"] == "foreign"

    def test_both_failures_are_reachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``__cause__`` is the restore failure — the marker
        ``skills._promote_race_conflict`` reads to refuse demoting this state to
        an ordinary skip — and the promotion failure travels in ``original``."""
        exc = self._run(tmp_path, monkeypatch)
        assert isinstance(exc.__cause__, OSError)
        assert exc.__cause__.errno in (errno.EEXIST, errno.ENOTEMPTY)
        assert isinstance(exc.original, OSError)
        assert exc.original.errno == errno.EIO
        assert exc.errno == errno.EBUSY

    def test_marker_still_owns_the_staging_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The signal a caller's ``finally`` must consult — a disk fact, so it
        also holds for a SIGKILL that leaves no exception at all."""
        self._run(tmp_path, monkeypatch)
        assert marker_owns_staging(_paths(tmp_path)["staging"]) is True

    def test_recovery_leaves_it_at_row_4(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression being pinned: if anything deleted the staging tree
        this would classify as row 3, whose action deletes ``old``."""
        self._run(tmp_path, monkeypatch)
        p = _paths(tmp_path)

        with pytest.raises(SwapForeignDestination):
            recover_pending_swaps(tmp_path, "skill")

        assert _read_tree(p["old"])["SKILL.md"] == "old"
        assert _read_tree(p["staging"])["SKILL.md"] == "new"
        assert p["marker"].is_file()


class TestUnwindOrdering:
    def test_fsync_falls_between_the_restore_and_the_marker_unlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a single trailing flush a power cut could persist the marker
        deletion while losing the restore rename, leaving an UNMARKED state the
        recovery machine can no longer see. The marker must outlive the state
        it describes."""
        p = _paths(tmp_path)
        dst = _mkdir_tree(p["dst"], "old")
        staging = _mkdir_tree(p["staging"], "new")
        events: list[str] = []
        real_rename = _dir_swap.rename_no_replace
        real_unlink = Path.unlink

        def fail_promote(src: Path, target: Path) -> None:
            if src == p["staging"]:
                raise OSError(errno.EIO, "promote failed")
            real_rename(src, target)
            events.append(f"rename:{src.name}->{target.name}")

        def unlink_spy(self: Path, *args: object, **kwargs: object) -> None:
            if self == p["marker"]:
                events.append("unlink-marker")
            real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(_dir_swap, "rename_no_replace", fail_promote)
        monkeypatch.setattr(_dir_swap, "fsync_dir", lambda path: events.append("fsync") or True)
        monkeypatch.setattr(Path, "unlink", unlink_spy)

        with pytest.raises(OSError):
            swap_dir_tree(staging, dst)

        restore = events.index(f"rename:{p['old'].name}->{p['dst'].name}")
        unlink = events.index("unlink-marker")
        assert restore < unlink
        assert "fsync" in events[restore + 1 : unlink]


class TestRecoveryRows:
    """Every row of the §5 table, by constructing the on-disk state directly."""

    def test_row1_discards_the_staging_tree(self, tmp_path: Path) -> None:
        p = _paths(tmp_path)
        _mkdir_tree(p["dst"], "original")
        _mkdir_tree(p["staging"], "candidate")
        _write_marker(tmp_path)

        assert recover_pending_swaps(tmp_path, "skill") is True
        assert _read_tree(p["dst"])["SKILL.md"] == "original"
        assert _residue(tmp_path) == []

    def test_row2_completes_forward(self, tmp_path: Path) -> None:
        """The marker is written only after staging is complete, and the
        pre-image is already snapshotted, so forwarding loses nothing."""
        p = _paths(tmp_path)
        _mkdir_tree(p["old"], "original")
        _mkdir_tree(p["staging"], "candidate")
        _write_marker(tmp_path)

        assert recover_pending_swaps(tmp_path, "skill") is True
        assert _read_tree(p["dst"])["SKILL.md"] == "candidate"
        assert _residue(tmp_path) == []

    def test_row3_finishes_the_cleanup(self, tmp_path: Path) -> None:
        p = _paths(tmp_path)
        _mkdir_tree(p["dst"], "promoted")
        _mkdir_tree(p["old"], "original")
        _write_marker(tmp_path)

        assert recover_pending_swaps(tmp_path, "skill") is True
        assert _read_tree(p["dst"])["SKILL.md"] == "promoted"
        assert _residue(tmp_path) == []

    def test_row4_fails_closed_and_names_both_trees(self, tmp_path: Path) -> None:
        """Provenance is genuinely ambiguous: either ``dst`` was recreated
        mid-swap (making ``old`` the original) or rename 1 hit a foreign ``old``
        and the process died before unwinding (making ``dst`` the original).
        Claiming either would talk an operator into deleting the good tree."""
        p = _paths(tmp_path)
        _mkdir_tree(p["dst"], "candidate-a")
        _mkdir_tree(p["old"], "candidate-b")
        _mkdir_tree(p["staging"], "candidate-c")
        _write_marker(tmp_path)

        with pytest.raises(SwapForeignDestination) as exc:
            recover_pending_swaps(tmp_path, "skill")

        message = str(exc.value)
        assert str(p["dst"]) in message
        assert str(p["old"]) in message
        assert "AMBIGUOUS" in message
        assert _read_tree(p["dst"])["SKILL.md"] == "candidate-a"
        assert _read_tree(p["old"])["SKILL.md"] == "candidate-b"
        assert _read_tree(p["staging"])["SKILL.md"] == "candidate-c"
        assert p["marker"].is_file()

    def test_row5_rolls_back(self, tmp_path: Path) -> None:
        p = _paths(tmp_path)
        _mkdir_tree(p["old"], "original")
        _write_marker(tmp_path)

        assert recover_pending_swaps(tmp_path, "skill") is True
        assert _read_tree(p["dst"])["SKILL.md"] == "original"
        assert _residue(tmp_path) == []

    def test_row6_forwards_with_no_pre_image(self, tmp_path: Path) -> None:
        p = _paths(tmp_path)
        _mkdir_tree(p["staging"], "candidate")
        _write_marker(tmp_path)

        assert recover_pending_swaps(tmp_path, "skill") is True
        assert _read_tree(p["dst"])["SKILL.md"] == "candidate"
        assert _residue(tmp_path) == []

    def test_row7_drops_a_stale_marker(self, tmp_path: Path) -> None:
        p = _paths(tmp_path)
        _mkdir_tree(p["dst"], "promoted")
        _write_marker(tmp_path)

        assert recover_pending_swaps(tmp_path, "skill") is True
        assert _read_tree(p["dst"])["SKILL.md"] == "promoted"
        assert _residue(tmp_path) == []

    def test_row8_drops_a_stale_marker(self, tmp_path: Path) -> None:
        _write_marker(tmp_path)
        assert recover_pending_swaps(tmp_path, "skill") is True
        assert _residue(tmp_path) == []

    def test_no_marker_is_not_recovery(self, tmp_path: Path) -> None:
        _mkdir_tree(tmp_path / "skill", "canonical")
        assert recover_pending_swaps(tmp_path, "skill") is False

    def test_absent_root_is_not_recovery(self, tmp_path: Path) -> None:
        assert recover_pending_swaps(tmp_path / "nope", "skill") is False

    def test_a_destination_recreated_mid_recovery_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Row 5's restore is exclusive: someone who recreated ``dst`` between
        the classification and the rename keeps their tree."""
        p = _paths(tmp_path)
        _mkdir_tree(p["old"], "original")
        _write_marker(tmp_path)
        real_rename = _dir_swap.rename_no_replace

        def recreate_first(src: Path, target: Path) -> None:
            _mkdir_tree(p["dst"], "foreign")
            real_rename(src, target)

        monkeypatch.setattr(_dir_swap, "rename_no_replace", recreate_first)
        with pytest.raises(SwapForeignDestination):
            recover_pending_swaps(tmp_path, "skill")

        assert _read_tree(p["dst"])["SKILL.md"] == "foreign"
        assert _read_tree(p["old"])["SKILL.md"] == "original"

    def test_fsync_dir_unsupported_does_not_fail_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = _paths(tmp_path)
        _mkdir_tree(p["old"], "original")
        _mkdir_tree(p["staging"], "candidate")
        _write_marker(tmp_path)
        monkeypatch.setattr(_dir_swap, "fsync_dir", lambda path: False)

        assert recover_pending_swaps(tmp_path, "skill") is True
        assert _read_tree(p["dst"])["SKILL.md"] == "candidate"


class TestRecoveryTypeGate:
    """Classifying by existence alone would let a forwarding row rename a
    regular file, a symlink or a device node into the canonical position."""

    @pytest.mark.parametrize("slot", ["dst", "old", "staging"])
    def test_regular_file_in_a_transient_slot_fails_closed(self, tmp_path: Path, slot: str) -> None:
        p = _paths(tmp_path)
        _mkdir_tree(p["old"] if slot != "old" else p["staging"], "original")
        p[slot].write_text("not a tree", encoding="utf-8")
        _write_marker(tmp_path)

        with pytest.raises(SwapRecoveryError):
            recover_pending_swaps(tmp_path, "skill")
        assert p[slot].is_file()
        assert p["marker"].exists()

    @pytest.mark.requires_symlinks
    @pytest.mark.parametrize("slot", ["dst", "old", "staging"])
    def test_symlink_in_a_transient_slot_fails_closed(self, tmp_path: Path, slot: str) -> None:
        p = _paths(tmp_path)
        target = _mkdir_tree(tmp_path / "elsewhere", "linked")
        p[slot].symlink_to(target, target_is_directory=True)
        _write_marker(tmp_path)

        with pytest.raises(SwapRecoveryError):
            recover_pending_swaps(tmp_path, "skill")
        assert p[slot].is_symlink()
        assert _read_tree(target)["SKILL.md"] == "linked"

    @_requires_fifo
    @pytest.mark.parametrize("slot", ["dst", "old", "staging"])
    def test_fifo_in_a_transient_slot_fails_closed(self, tmp_path: Path, slot: str) -> None:
        p = _paths(tmp_path)
        os.mkfifo(p[slot])
        _write_marker(tmp_path)

        with _deadline(5):
            with pytest.raises(SwapRecoveryError):
                recover_pending_swaps(tmp_path, "skill")
        assert stat.S_ISFIFO(os.lstat(p[slot]).st_mode)


class TestMarkerOwnsStaging:
    def test_true_only_while_the_marker_is_live(self, tmp_path: Path) -> None:
        p = _paths(tmp_path)
        _mkdir_tree(p["staging"], "candidate")
        assert marker_owns_staging(p["staging"]) is False

        _write_marker(tmp_path)
        assert marker_owns_staging(p["staging"]) is True

        p["marker"].unlink()
        assert marker_owns_staging(p["staging"]) is False

    def test_a_non_conforming_staging_name_is_never_claimed(self, tmp_path: Path) -> None:
        stray = tmp_path / ".staging-notes.tmp"
        stray.mkdir()
        assert marker_owns_staging(stray) is False

    @pytest.mark.requires_symlinks
    def test_a_symlinked_marker_does_not_count_as_a_claim(self, tmp_path: Path) -> None:
        """The probe answers "is a real marker here", not "does something exist
        with that name" — the same fail-closed reading the marker load uses."""
        p = _paths(tmp_path)
        _mkdir_tree(p["staging"], "candidate")
        target = tmp_path / "elsewhere.json"
        target.write_text("{}", encoding="utf-8")
        p["marker"].symlink_to(target)

        assert marker_owns_staging(p["staging"]) is False


class TestNoCallerYet:
    def test_the_primitive_exists_but_skills_overwrite_is_still_refused(self) -> None:
        """PR-G4a-2 ships the swap primitive and wires NO caller — PR-G4b's
        history-preserving transaction is the only thing that will ever call it.

        Pinned so "the swap protocol exists" can never be mistaken for "skills
        overwrite works". The behavioural half of this pin lives in
        ``test_context_pull_apply.py``; this half asserts the module is
        importable without changing any engine decision.
        """
        from memtomem.context import pull_apply

        assert callable(swap_dir_tree)
        assert callable(recover_pending_swaps)
        assert "skills_overwrite_unsupported" in str(pull_apply.PullApplyStatus)
