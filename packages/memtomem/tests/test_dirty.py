"""Tests for ``memtomem.context.dirty`` and ``Lockfile.iter_entries``.

Covers PR-D C2 commit 2: dirty classification rules feeding ``mm context
update``, plus the lockfile iteration helper introduced for batch flows.

These tests construct lockfile + dest tree manually (no wiki / install
roundtrip) so the dirty classifier is exercised in isolation. The
"installed_at captured from filesystem after writes" invariant from
ADR-0008 PR-B / C2a / #634
(:func:`memtomem.context.install._install_asset` →
:func:`memtomem.context._atomic.installed_at_from_dest`) is mirrored by
:func:`_setup_installed` so equality cases land on the clean side of
the strict ``>`` boundary on every platform.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memtomem.context._atomic import installed_at_from_dest
from memtomem.context.dirty import is_asset_dirty
from memtomem.context.lockfile import Lockfile


# ── helpers ──────────────────────────────────────────────────────────────


def _setup_installed(
    project: Path,
    asset_type: str,
    name: str,
    files: dict[str, bytes],
) -> str:
    """Drop *files* into ``<project>/.memtomem/<asset_type>/<name>/`` and
    write a lockfile entry whose ``installed_at`` is captured AFTER all
    file writes — mirrors C2a's invariant so a clean check immediately
    after this helper returns lands at ``mtime <= installed_at_epoch``.
    """
    dest = project / ".memtomem" / asset_type / name
    dest.mkdir(parents=True)
    for relpath, data in files.items():
        target = dest / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    installed_at = installed_at_from_dest(dest)
    Lockfile.at(project).upsert_entry(
        asset_type,
        name,
        wiki_commit="0" * 40,
        installed_at=installed_at,
    )
    return installed_at


def _bump_mtime(path: Path, *, seconds_in_future: float = 1.0) -> None:
    """Force *path*'s mtime to ``now + seconds_in_future``.

    Tests need a deterministic strict ``>`` margin against ``installed_at``
    that can't be eaten by single-second filesystem precision. Bumping
    mtime explicitly avoids ``time.sleep`` in unit tests.
    """
    future = datetime.now(timezone.utc).timestamp() + seconds_in_future
    os.utime(path, (future, future))


# ── is_asset_dirty: clean / dirty paths ──────────────────────────────────


def test_clean_immediately_after_install(tmp_path: Path) -> None:
    """Files at install-time mtime are ``<= installed_at_epoch`` (C2a invariant)."""
    _setup_installed(
        tmp_path,
        "skills",
        "foo",
        {"SKILL.md": b"# foo\n", "scripts/run.sh": b"#!/bin/bash\n"},
    )

    report = is_asset_dirty(tmp_path, "skills", "foo")

    assert report.reason == "clean"
    assert report.dirty_files == ()
    assert report.checked_files == 2
    assert report.installed_at is not None


def test_dirty_after_edit(tmp_path: Path) -> None:
    """A post-install edit produces ``reason='dirty'`` with the file flagged."""
    _setup_installed(tmp_path, "skills", "foo", {"SKILL.md": b"original\n"})

    edited = tmp_path / ".memtomem" / "skills" / "foo" / "SKILL.md"
    edited.write_bytes(b"manual edit\n")
    _bump_mtime(edited)

    report = is_asset_dirty(tmp_path, "skills", "foo")

    assert report.reason == "dirty"
    assert edited in report.dirty_files
    assert report.checked_files == 1


def test_dirty_partial_subdir(tmp_path: Path) -> None:
    """Only the edited file is flagged; siblings stay clean."""
    _setup_installed(
        tmp_path,
        "skills",
        "foo",
        {
            "SKILL.md": b"a",
            "scripts/run.sh": b"b",
            "scripts/helper.py": b"c",
            "overrides/claude.md": b"d",
        },
    )

    edited = tmp_path / ".memtomem" / "skills" / "foo" / "scripts" / "run.sh"
    edited.write_bytes(b"edited\n")
    _bump_mtime(edited)

    report = is_asset_dirty(tmp_path, "skills", "foo")

    assert report.reason == "dirty"
    assert report.dirty_files == (edited,)
    assert report.checked_files == 4


# ── never_installed / missing_dest ───────────────────────────────────────


def test_never_installed(tmp_path: Path) -> None:
    """No lockfile entry → reason='never_installed' with empty fields."""
    report = is_asset_dirty(tmp_path, "skills", "missing")

    assert report.reason == "never_installed"
    assert report.installed_at is None
    assert report.dirty_files == ()
    assert report.checked_files == 0


def test_missing_dest(tmp_path: Path) -> None:
    """Lockfile entry exists but the dest dir was deleted out from under it."""
    installed_at = _setup_installed(tmp_path, "skills", "foo", {"SKILL.md": b"x"})
    shutil.rmtree(tmp_path / ".memtomem" / "skills" / "foo")

    report = is_asset_dirty(tmp_path, "skills", "foo")

    assert report.reason == "missing_dest"
    assert report.installed_at == installed_at
    assert report.dirty_files == ()
    assert report.checked_files == 0


# ── skip rules: COPY_SKIP_NAMES / symlinks ───────────────────────────────


def test_skip_dotgit_dsstore_pycache(tmp_path: Path) -> None:
    """COPY_SKIP_NAMES entries with *future* mtime do not flip clean→dirty."""
    _setup_installed(tmp_path, "skills", "foo", {"SKILL.md": b"x"})
    dest = tmp_path / ".memtomem" / "skills" / "foo"

    # All three injected with future mtime: would absolutely be dirty if checked.
    (dest / ".DS_Store").write_bytes(b"\x00" * 4)
    _bump_mtime(dest / ".DS_Store")

    git_dir = dest / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    _bump_mtime(git_dir / "HEAD")

    pycache = dest / "__pycache__"
    pycache.mkdir()
    (pycache / "foo.cpython-312.pyc").write_bytes(b"\x00\x00")
    _bump_mtime(pycache / "foo.cpython-312.pyc")

    report = is_asset_dirty(tmp_path, "skills", "foo")

    assert report.reason == "clean"
    assert report.dirty_files == ()
    assert report.checked_files == 1  # only SKILL.md


def test_skip_symlinks(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Symlinks in dest are skipped with a warning, never dereferenced."""
    _setup_installed(tmp_path, "skills", "foo", {"SKILL.md": b"x"})
    dest = tmp_path / ".memtomem" / "skills" / "foo"

    # Dangling symlink — entry.is_symlink() fires regardless of target validity.
    (dest / "danger.md").symlink_to("/nonexistent/target")

    with caplog.at_level("WARNING", logger="memtomem.context.dirty"):
        report = is_asset_dirty(tmp_path, "skills", "foo")

    assert report.reason == "clean"
    assert report.checked_files == 1
    assert any("skipping symlink" in r.message for r in caplog.records)


def test_skip_dot_bak_files(tmp_path: Path) -> None:
    """``.bak`` siblings (from prior ``--force``) do NOT trip the next
    update into ``reason='dirty'``.

    Regression for the manual-smoke finding: scenario 3 of the PR-D C2
    smoke run left ``SKILL.md.bak`` carrying the user's pre-update mtime,
    which then caused scenario 4's ``--all`` classification to refuse
    proj-a forever. ``.bak`` files are intentional artifacts of the
    update flow itself; they must not feed back into dirty detection.
    """
    _setup_installed(tmp_path, "skills", "foo", {"SKILL.md": b"x"})
    dest = tmp_path / ".memtomem" / "skills" / "foo"

    bak = dest / "SKILL.md.bak"
    bak.write_bytes(b"prior dirty edit")
    _bump_mtime(bak)  # absolutely future mtime — would 100% be dirty if checked

    report = is_asset_dirty(tmp_path, "skills", "foo")

    assert report.reason == "clean"
    assert report.dirty_files == ()
    assert report.checked_files == 1  # SKILL.md only — SKILL.md.bak skipped


# ── Lockfile.iter_entries ────────────────────────────────────────────────


def test_iter_entries_alphabetical_ordering(tmp_path: Path) -> None:
    """``iter_entries`` yields ``(asset_type, name)`` in alphabetical order
    regardless of insertion order — deterministic for batch flows."""
    lock = Lockfile.at(tmp_path)

    # Insert deliberately out of order, mixing asset types.
    lock.upsert_entry(
        "skills", "zebra", wiki_commit="0" * 40, installed_at="2026-01-01T00:00:00.000000Z"
    )
    lock.upsert_entry(
        "agents", "alpha", wiki_commit="0" * 40, installed_at="2026-01-01T00:00:00.000000Z"
    )
    lock.upsert_entry(
        "commands", "mid", wiki_commit="0" * 40, installed_at="2026-01-01T00:00:00.000000Z"
    )
    lock.upsert_entry(
        "skills", "alpha", wiki_commit="0" * 40, installed_at="2026-01-01T00:00:00.000000Z"
    )

    entries = list(lock.iter_entries())
    types_and_names = [(t, n) for t, n, _ in entries]

    assert types_and_names == [
        ("agents", "alpha"),
        ("commands", "mid"),
        ("skills", "alpha"),
        ("skills", "zebra"),
    ]
    # Per-entry payload is the live dict — value must round-trip through.
    assert all(e["wiki_commit"] == "0" * 40 for _, _, e in entries)


# ── installed_at_from_dest: capture helper (#634) ────────────────────────


class TestInstalledAtFromDest:
    """Pin :func:`memtomem.context._atomic.installed_at_from_dest`.

    Two-layer fix for the Windows dirty-cluster (#634):

    1. Capture from the filesystem (not Python's wall clock).
    2. Ceiling-divide ``st_mtime_ns`` to microseconds so the formatted
       ISO-8601Z round-trips ``>=`` every walked file's actual mtime.

    Each test exercises one or both layers. Mutation validation lives in
    the PR description: reverting just the ceiling (``max_us = max_ns //
    1000``) makes :meth:`test_us_residual_round_trips_monotonically`
    fail; reverting capture to ``utcnow_iso8601_z()`` makes
    :meth:`test_multi_file_returns_max_of_actual_mtimes` fail.
    """

    ISO_8601Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

    @staticmethod
    def _round_trip(ts: str) -> float:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()

    def test_empty_dest_falls_back_to_wall_clock(self, tmp_path: Path) -> None:
        """No files → wall-clock fallback; format matches utcnow_iso8601_z."""
        dest = tmp_path / "empty"
        dest.mkdir()
        before = datetime.now(timezone.utc).timestamp()
        result = installed_at_from_dest(dest)
        after = datetime.now(timezone.utc).timestamp()
        assert self.ISO_8601Z_RE.match(result), result
        round_tripped = self._round_trip(result)
        assert before <= round_tripped <= after

    def test_single_file_round_trips_at_least_its_mtime(self, tmp_path: Path) -> None:
        """Captured installed_at parses back >= the file's actual st_mtime."""
        dest = tmp_path / "single"
        dest.mkdir()
        f = dest / "SKILL.md"
        f.write_bytes(b"x")
        round_tripped = self._round_trip(installed_at_from_dest(dest))
        assert round_tripped >= f.stat().st_mtime

    def test_multi_file_returns_max_of_actual_mtimes(self, tmp_path: Path) -> None:
        """With heterogeneous mtimes the result is >= the latest one.

        Reverting the helper to ``utcnow_iso8601_z()`` makes this fail —
        the bumped mtime is in the future relative to wall clock.
        """
        dest = tmp_path / "multi"
        dest.mkdir()
        a = dest / "a.md"
        b = dest / "b.md"
        a.write_bytes(b"a")
        b.write_bytes(b"b")
        future_ns = a.stat().st_mtime_ns + 5_000_000_000  # +5 seconds
        os.utime(b, ns=(future_ns, future_ns))
        round_tripped = self._round_trip(installed_at_from_dest(dest))
        assert round_tripped >= b.stat().st_mtime
        assert round_tripped >= a.stat().st_mtime

    def test_skip_rules_excluded_from_max(self, tmp_path: Path) -> None:
        """``.git``, ``.DS_Store``, ``__pycache__``, ``.bak`` are ignored.

        Each skipped entry gets a far-future mtime; if any leaked into the
        ``max`` the result would round-trip past it.
        """
        dest = tmp_path / "skip"
        dest.mkdir()
        canonical = dest / "SKILL.md"
        canonical.write_bytes(b"canonical")
        skipped = [
            dest / "SKILL.md.bak",
            dest / ".DS_Store",
            dest / ".git" / "config",
            dest / "__pycache__" / "x.pyc",
        ]
        for s in skipped:
            s.parent.mkdir(parents=True, exist_ok=True)
            s.write_bytes(b"skip")
        future_ns = canonical.stat().st_mtime_ns + 60_000_000_000  # +60s
        for s in skipped:
            os.utime(s, ns=(future_ns, future_ns))
        round_tripped = self._round_trip(installed_at_from_dest(dest))
        assert round_tripped < future_ns / 1_000_000_000
        assert round_tripped >= canonical.stat().st_mtime

    def test_us_residual_round_trips_monotonically(self, tmp_path: Path) -> None:
        """The microsecond ceiling is load-bearing.

        Models NTFS's 100-ns residual via ``os.utime(..., ns=base+750)``.
        Truncating to µs (``max_ns // 1000``) would format ``base`` and
        parse back to a value strictly less than the file's actual
        ``st_mtime``, defeating the strict ``>`` invariant in
        ``dirty.py:is_asset_dirty`` on the install's own writes. The
        ceiling rounds up so round-trip stays monotonic.

        On filesystems that quantise to µs (older ext4 / FAT-class
        targets) ``os.utime`` rounds the ns down and the residual is 0
        — the ceiling is then a no-op and the assertion still holds.
        """
        dest = tmp_path / "residual"
        dest.mkdir()
        f = dest / "a.md"
        f.write_bytes(b"x")
        base_ns = f.stat().st_mtime_ns - (f.stat().st_mtime_ns % 1000)
        target_ns = base_ns + 750
        os.utime(f, ns=(target_ns, target_ns))
        result = installed_at_from_dest(dest)
        round_tripped = self._round_trip(result)
        actual_mtime = f.stat().st_mtime
        assert round_tripped >= actual_mtime, (
            f"installed_at {result} (round-tripped to {round_tripped}) is "
            f"strictly less than the file's just-captured mtime "
            f"{actual_mtime} ({f.stat().st_mtime_ns}ns) — the µs ceiling "
            "regressed."
        )


# ── B1: manifest-based deletion-dirty (#1247 item 12) ────────────────────


def _setup_installed_with_manifest(
    project: Path,
    asset_type: str,
    name: str,
    files: dict[str, bytes],
) -> str:
    """Like :func:`_setup_installed` but records the B1 file manifest
    (``files`` + ``files_commit`` matching ``wiki_commit``)."""
    dest = project / ".memtomem" / asset_type / name
    dest.mkdir(parents=True)
    for relpath, data in files.items():
        target = dest / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    installed_at = installed_at_from_dest(dest)
    Lockfile.at(project).upsert_entry(
        asset_type,
        name,
        wiki_commit="0" * 40,
        installed_at=installed_at,
        files=sorted(files),
        files_commit="0" * 40,
    )
    return installed_at


def test_user_deleted_file_reports_missing(tmp_path: Path) -> None:
    """A manifest entry absent from disk classifies the asset dirty with the
    relpath in ``missing_files`` — pre-B1 this was the guaranteed false
    negative (clean → silent resurrection on the next update)."""
    _setup_installed_with_manifest(
        tmp_path, "skills", "web", {"SKILL.md": b"v1\n", "scripts/run.py": b"r\n"}
    )

    (tmp_path / ".memtomem" / "skills" / "web" / "scripts" / "run.py").unlink()

    report = is_asset_dirty(tmp_path, "skills", "web")
    assert report.reason == "dirty"
    assert report.dirty_files == ()
    assert [str(p) for p in report.missing_files] == [
        str(tmp_path / ".memtomem" / "skills" / "web" / "scripts" / "run.py")
    ]


def test_intact_manifest_stays_clean(tmp_path: Path) -> None:
    """Negative pin: manifest present and every file on disk → clean."""
    _setup_installed_with_manifest(tmp_path, "skills", "web", {"SKILL.md": b"v1\n"})

    report = is_asset_dirty(tmp_path, "skills", "web")
    assert report.reason == "clean"
    assert report.missing_files == ()


@pytest.mark.parametrize(
    "files_value, files_commit_value",
    [
        ("not-a-list", "0" * 40),  # files not a list
        ([42], "0" * 40),  # non-str member
        (["../escape.md"], "0" * 40),  # path traversal
        (["/abs.md"], "0" * 40),  # absolute path
        (["a\\b.md"], "0" * 40),  # backslash separator
        (["SKILL.md"], "f" * 40),  # files_commit mismatch
        (["SKILL.md"], None),  # files_commit missing
    ],
)
def test_malformed_or_stale_manifest_ignored(
    tmp_path: Path, files_value: object, files_commit_value: object
) -> None:
    """Codex design-gate M3: lock.json is git-tracked and hand-mergeable —
    any malformed manifest shape degrades to pre-B1 semantics (no manifest),
    never a crash or a wrong membership check."""
    _setup_installed(tmp_path, "skills", "web", {"SKILL.md": b"v1\n"})

    lock_path = tmp_path / ".memtomem" / "lock.json"
    doc = json.loads(lock_path.read_text(encoding="utf-8"))
    entry = doc["skills"]["web"]
    entry["files"] = files_value
    if files_commit_value is not None:
        entry["files_commit"] = files_commit_value
    lock_path.write_text(json.dumps(doc), encoding="utf-8")

    report = is_asset_dirty(tmp_path, "skills", "web")
    assert report.reason == "clean"
    assert report.missing_files == ()


# ── malformed installed_at degrades, never crashes (#1247 id 1) ──────────


def _corrupt_installed_at(project: Path, asset_type: str, name: str, value: str) -> None:
    lock_path = project / ".memtomem" / "lock.json"
    doc = json.loads(lock_path.read_text(encoding="utf-8"))
    doc[asset_type][name]["installed_at"] = value
    lock_path.write_text(json.dumps(doc), encoding="utf-8")


@pytest.mark.parametrize("malformed", ["yesterday", "2026-05-1", ""])
def test_malformed_installed_at_degrades_to_never_installed(tmp_path: Path, malformed: str) -> None:
    """An unparseable installed_at STRING degrades exactly like its
    missing/non-string siblings (#1247 id 1) — previously
    ``datetime.fromisoformat`` raised an uncaught ValueError whenever the
    dest dir existed, crashing status/update/install-all classification."""
    _setup_installed(tmp_path, "skills", "web", {"SKILL.md": b"v1\n"})
    _corrupt_installed_at(tmp_path, "skills", "web", malformed)

    report = is_asset_dirty(tmp_path, "skills", "web")

    assert report.reason == "never_installed"
    assert report.installed_at is None
    assert report.dirty_files == ()
    assert report.checked_files == 0


def test_malformed_installed_at_with_missing_dest_degrades_too(tmp_path: Path) -> None:
    """Consistency half of #1247 id 1: malformed + dest gone is also
    never_installed (it used to return missing_dest because the dest probe
    ran before the parse) — malformed ≡ non-string in EVERY branch."""
    _setup_installed(tmp_path, "skills", "web", {"SKILL.md": b"v1\n"})
    shutil.rmtree(tmp_path / ".memtomem" / "skills" / "web")
    _corrupt_installed_at(tmp_path, "skills", "web", "yesterday")

    report = is_asset_dirty(tmp_path, "skills", "web")

    assert report.reason == "never_installed"
    assert report.installed_at is None
