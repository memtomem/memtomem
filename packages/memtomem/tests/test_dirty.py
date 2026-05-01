"""Tests for ``memtomem.context.dirty`` and ``Lockfile.iter_entries``.

Covers PR-D C2 commit 2: dirty classification rules feeding ``mm context
update``, plus the lockfile iteration helper introduced for batch flows.

These tests construct lockfile + dest tree manually (no wiki / install
roundtrip) so the dirty classifier is exercised in isolation. The
"installed_at captured post-write" invariant from ADR-0008 PR-B/C2a
(:func:`memtomem.context.install._install_asset`) is mirrored by
:func:`_setup_installed` so equality cases land on the clean side of
the strict ``>`` boundary.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memtomem.context.dirty import is_asset_dirty
from memtomem.context.lockfile import Lockfile, utcnow_iso8601_z


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

    installed_at = utcnow_iso8601_z()
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
