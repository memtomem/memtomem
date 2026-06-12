"""Tests for ``memtomem.context.migrate`` and the ``mm context migrate`` CLI.

Covers PR-D C4: flat → dir layout normalization. The truth table in the
plan file has eight rows (`flat? × dir? × lock? × dirty?`); each is
exercised here as a unit test. CLI integration tests use ``CliRunner``
and stand on the same fixtures as the unit tests.

No wiki involvement — migrate is a pure filesystem + lockfile operation
(ADR-0008 Invariants 1 / 3), so unlike ``test_context_status`` we don't
need a ``wiki_root`` fixture.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context as context_group
from memtomem.context._names import InvalidNameError
from memtomem.context.lockfile import Lockfile, utcnow_iso8601_z
from memtomem.context.migrate import (
    MigrateRow,
    _is_flat_file_dirty,
    adopt_flat_to_dir,
    classify_migrate,
    migrate_one,
)


# ── helpers ──────────────────────────────────────────────────────────────


_ASSET_DIR_FILES = {"agents": "agent.md", "commands": "command.md"}


def _write_flat(project: Path, asset_type: str, name: str, body: bytes) -> Path:
    target = project / ".memtomem" / asset_type / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def _write_dir(project: Path, asset_type: str, name: str, body: bytes) -> Path:
    target = project / ".memtomem" / asset_type / name / _ASSET_DIR_FILES[asset_type]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def _add_lock_entry(project: Path, asset_type: str, name: str) -> str:
    installed_at = utcnow_iso8601_z()
    Lockfile.at(project).upsert_entry(
        asset_type, name, wiki_commit="0" * 40, installed_at=installed_at
    )
    return installed_at


def _bump_mtime(path: Path, *, seconds_in_future: float = 60.0) -> None:
    future = datetime.now(timezone.utc).timestamp() + seconds_in_future
    os.utime(path, (future, future))


# ── _is_flat_file_dirty ──────────────────────────────────────────────────


def test_is_flat_file_dirty_strict_gt(tmp_path: Path) -> None:
    """Equality with installed_at is clean; only strictly newer is dirty."""
    flat = _write_flat(tmp_path, "agents", "foo", b"v1\n")
    installed_at = _add_lock_entry(tmp_path, "agents", "foo")

    # Identical mtime → clean (mirrors dirty.py contract).
    epoch = datetime.fromisoformat(installed_at).timestamp()
    os.utime(flat, (epoch, epoch))
    entry = Lockfile.at(tmp_path).read_entry("agents", "foo")
    assert entry is not None
    assert _is_flat_file_dirty(flat, entry) is False

    # Strictly later → dirty.
    _bump_mtime(flat)
    assert _is_flat_file_dirty(flat, entry) is True


def test_is_flat_file_dirty_missing_installed_at_returns_false(tmp_path: Path) -> None:
    flat = _write_flat(tmp_path, "agents", "foo", b"v1\n")
    assert _is_flat_file_dirty(flat, {}) is False
    assert _is_flat_file_dirty(flat, {"installed_at": 123}) is False


# ── classify_migrate (8-row truth table) ─────────────────────────────────


def test_classify_flat_only_with_lock_clean(tmp_path: Path) -> None:
    """Row 1: flat ✓, dir ✗, lock ✓, clean → state=migrate."""
    flat = _write_flat(tmp_path, "agents", "foo", b"v1\n")
    installed_at = _add_lock_entry(tmp_path, "agents", "foo")
    epoch = datetime.fromisoformat(installed_at).timestamp()
    os.utime(flat, (epoch, epoch))

    rows = classify_migrate(tmp_path)

    assert len(rows) == 1
    assert rows[0].state == "migrate"
    assert rows[0].flat_dirty is False
    assert rows[0].asset_type == "agents"
    assert rows[0].name == "foo"


def test_classify_flat_only_with_lock_dirty(tmp_path: Path) -> None:
    """Row 2: flat ✓, dir ✗, lock ✓, dirty → state=refuse_dirty."""
    flat = _write_flat(tmp_path, "agents", "foo", b"v1\n")
    _add_lock_entry(tmp_path, "agents", "foo")
    _bump_mtime(flat)

    rows = classify_migrate(tmp_path)

    assert len(rows) == 1
    assert rows[0].state == "refuse_dirty"
    assert rows[0].flat_dirty is True


def test_classify_flat_only_no_lock(tmp_path: Path) -> None:
    """Row 3: flat ✓, dir ✗, lock ✗ → state=skip_manual."""
    _write_flat(tmp_path, "agents", "foo", b"v1\n")

    rows = classify_migrate(tmp_path)

    assert len(rows) == 1
    assert rows[0].state == "skip_manual"
    assert rows[0].flat_dirty is None
    assert rows[0].has_lock_entry is False


def test_classify_dir_only(tmp_path: Path) -> None:
    """Row 4: flat ✗, dir ✓ → state=noop (already migrated)."""
    _write_dir(tmp_path, "agents", "foo", b"v1\n")
    _add_lock_entry(tmp_path, "agents", "foo")

    rows = classify_migrate(tmp_path)

    assert len(rows) == 1
    assert rows[0].state == "noop"


def test_classify_flat_plus_dir_clean(tmp_path: Path) -> None:
    """Row 5: flat ✓, dir ✓, lock ✓, clean → state=cleanup_flat."""
    flat = _write_flat(tmp_path, "agents", "foo", b"flat-bytes\n")
    _write_dir(tmp_path, "agents", "foo", b"dir-bytes\n")
    installed_at = _add_lock_entry(tmp_path, "agents", "foo")
    epoch = datetime.fromisoformat(installed_at).timestamp()
    os.utime(flat, (epoch, epoch))

    rows = classify_migrate(tmp_path)

    assert len(rows) == 1
    assert rows[0].state == "cleanup_flat"
    assert rows[0].flat_dirty is False


def test_classify_flat_plus_dir_dirty(tmp_path: Path) -> None:
    """Row 6: flat ✓, dir ✓, lock ✓, dirty → state=refuse_dirty."""
    flat = _write_flat(tmp_path, "agents", "foo", b"flat-bytes\n")
    _write_dir(tmp_path, "agents", "foo", b"dir-bytes\n")
    _add_lock_entry(tmp_path, "agents", "foo")
    _bump_mtime(flat)

    rows = classify_migrate(tmp_path)

    assert len(rows) == 1
    assert rows[0].state == "refuse_dirty"
    assert rows[0].dir_exists is True


def test_classify_flat_plus_dir_no_lock(tmp_path: Path) -> None:
    """Row 7: flat ✓, dir ✓, lock ✗ → state=skip_manual."""
    _write_flat(tmp_path, "agents", "foo", b"flat\n")
    _write_dir(tmp_path, "agents", "foo", b"dir\n")

    rows = classify_migrate(tmp_path)

    assert len(rows) == 1
    assert rows[0].state == "skip_manual"
    assert "collides" in rows[0].reason or "collision" in rows[0].reason


def test_classify_orphan_lockfile_entry(tmp_path: Path) -> None:
    """Row 8 (extension): flat ✗, dir ✗, lock ✓ → state=skip_orphan."""
    (tmp_path / ".memtomem").mkdir()
    _add_lock_entry(tmp_path, "agents", "foo")

    rows = classify_migrate(tmp_path)

    assert len(rows) == 1
    assert rows[0].state == "skip_orphan"


def test_classify_neither_no_rows(tmp_path: Path) -> None:
    """Row 9 (extension): flat ✗, dir ✗, lock ✗ → no rows."""
    (tmp_path / ".memtomem").mkdir()

    rows = classify_migrate(tmp_path)

    assert rows == []


def test_classify_skills_returns_empty(tmp_path: Path) -> None:
    """Skills are always dir layout — classification short-circuits."""
    rows = classify_migrate(tmp_path, asset_type="skills")
    assert rows == []


def test_classify_skills_with_name_returns_empty(tmp_path: Path) -> None:
    rows = classify_migrate(tmp_path, asset_type="skills", name="foo")
    assert rows == []


def test_classify_invalid_asset_type_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid asset_type"):
        classify_migrate(tmp_path, asset_type="bogus")


def test_classify_name_without_type_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="name requires asset_type"):
        classify_migrate(tmp_path, name="foo")


def test_classify_invalid_name_raises(tmp_path: Path) -> None:
    with pytest.raises(InvalidNameError):
        classify_migrate(tmp_path, asset_type="agents", name="../etc/passwd")


def test_classify_filters_to_named_asset(tmp_path: Path) -> None:
    _write_flat(tmp_path, "agents", "foo", b"a\n")
    _write_flat(tmp_path, "agents", "bar", b"b\n")
    _add_lock_entry(tmp_path, "agents", "foo")
    _add_lock_entry(tmp_path, "agents", "bar")

    rows = classify_migrate(tmp_path, asset_type="agents", name="foo")

    assert len(rows) == 1
    assert rows[0].name == "foo"


def test_classify_unknown_named_asset_returns_empty(tmp_path: Path) -> None:
    rows = classify_migrate(tmp_path, asset_type="agents", name="missing")
    assert rows == []


def test_classify_iterates_lockfile_union_disk(tmp_path: Path) -> None:
    """Ensure both lockfile-only and disk-only assets surface."""
    # disk-only (manual flat)
    _write_flat(tmp_path, "agents", "manual", b"a\n")
    # lockfile-only (orphan)
    _add_lock_entry(tmp_path, "agents", "ghost")

    rows = classify_migrate(tmp_path)
    states = {(r.name, r.state) for r in rows}

    assert ("manual", "skip_manual") in states
    assert ("ghost", "skip_orphan") in states


# ── migrate_one execution ────────────────────────────────────────────────


def test_migrate_one_atomic_rename_clean(tmp_path: Path) -> None:
    """state=migrate clean: flat → dir/agent.md, flat removed.

    Symmetric assertion (positive + negative markers per
    ``feedback_pin_invert_symmetric_assertion``):
    POSITIVE — dir/agent.md exists with the original bytes.
    NEGATIVE — flat path is gone.
    """
    flat = _write_flat(tmp_path, "agents", "foo", b"agent body\n")
    installed_at = _add_lock_entry(tmp_path, "agents", "foo")
    epoch = datetime.fromisoformat(installed_at).timestamp()
    os.utime(flat, (epoch, epoch))

    rows = classify_migrate(tmp_path)
    assert rows[0].state == "migrate"
    result = migrate_one(tmp_path, rows[0], force=False)

    target = tmp_path / ".memtomem" / "agents" / "foo" / "agent.md"
    # POSITIVE
    assert target.is_file()
    assert target.read_bytes() == b"agent body\n"
    # NEGATIVE
    assert not flat.exists()
    assert result.ok is True
    assert result.bak_path is None


def test_migrate_one_preserves_installed_at(tmp_path: Path) -> None:
    flat = _write_flat(tmp_path, "agents", "foo", b"v1\n")
    installed_at = _add_lock_entry(tmp_path, "agents", "foo")
    epoch = datetime.fromisoformat(installed_at).timestamp()
    os.utime(flat, (epoch, epoch))

    pre_entry = Lockfile.at(tmp_path).read_entry("agents", "foo")
    rows = classify_migrate(tmp_path)
    migrate_one(tmp_path, rows[0], force=False)

    post_entry = Lockfile.at(tmp_path).read_entry("agents", "foo")
    assert post_entry == pre_entry  # unchanged


def test_migrate_one_dirty_no_force_refuses(tmp_path: Path) -> None:
    flat = _write_flat(tmp_path, "agents", "foo", b"v1\n")
    _add_lock_entry(tmp_path, "agents", "foo")
    _bump_mtime(flat)

    rows = classify_migrate(tmp_path)
    result = migrate_one(tmp_path, rows[0], force=False)

    assert result.ok is False
    assert "force" in (result.error or "")
    assert flat.exists()  # unchanged
    assert not (tmp_path / ".memtomem" / "agents" / "foo" / "agent.md").exists()


def test_migrate_one_dirty_force_creates_bak(tmp_path: Path) -> None:
    """Dirty + --force: .bak written, then atomic migrate."""
    flat = _write_flat(tmp_path, "agents", "foo", b"user-edit\n")
    _add_lock_entry(tmp_path, "agents", "foo")
    _bump_mtime(flat)
    pre_mtime = flat.stat().st_mtime

    rows = classify_migrate(tmp_path)
    result = migrate_one(tmp_path, rows[0], force=True)

    assert result.ok is True
    assert result.bak_path is not None
    bak = result.bak_path
    assert bak.is_file()
    assert bak.read_bytes() == b"user-edit\n"
    # mtime preserved on the bak (shutil.copy2 contract)
    assert abs(bak.stat().st_mtime - pre_mtime) < 0.001
    # Migrated content lands in dir layout with the user's edit (not lost).
    target = tmp_path / ".memtomem" / "agents" / "foo" / "agent.md"
    assert target.read_bytes() == b"user-edit\n"
    assert not flat.exists()


def test_cleanup_flat_clean_preserves_dir(tmp_path: Path) -> None:
    """cleanup_flat clean: dir mtime/content unchanged, flat removed."""
    flat = _write_flat(tmp_path, "agents", "foo", b"flat-bytes\n")
    dir_target = _write_dir(tmp_path, "agents", "foo", b"dir-bytes\n")
    installed_at = _add_lock_entry(tmp_path, "agents", "foo")
    epoch = datetime.fromisoformat(installed_at).timestamp()
    os.utime(flat, (epoch, epoch))
    pre_dir_mtime = dir_target.stat().st_mtime
    pre_dir_bytes = dir_target.read_bytes()

    rows = classify_migrate(tmp_path)
    assert rows[0].state == "cleanup_flat"
    result = migrate_one(tmp_path, rows[0], force=False)

    assert result.ok is True
    assert not flat.exists()  # NEGATIVE: flat removed
    # POSITIVE: dir untouched
    assert dir_target.read_bytes() == pre_dir_bytes
    assert dir_target.stat().st_mtime == pre_dir_mtime


def test_cleanup_flat_dirty_force_keeps_dir_writes_bak(tmp_path: Path) -> None:
    """cleanup_flat dirty + force: .bak preserves user edits, dir untouched."""
    flat = _write_flat(tmp_path, "agents", "foo", b"flat-edit\n")
    dir_target = _write_dir(tmp_path, "agents", "foo", b"canonical\n")
    _add_lock_entry(tmp_path, "agents", "foo")
    _bump_mtime(flat)
    pre_dir_bytes = dir_target.read_bytes()

    rows = classify_migrate(tmp_path)
    assert rows[0].state == "refuse_dirty"  # collision + dirty
    result = migrate_one(tmp_path, rows[0], force=True)

    assert result.ok is True
    assert result.bak_path is not None
    assert result.bak_path.read_bytes() == b"flat-edit\n"
    assert not flat.exists()
    # Dir bytes unchanged — user edits live only in .bak (locked decision #10)
    assert dir_target.read_bytes() == pre_dir_bytes


def test_migrate_one_invalid_name_raises(tmp_path: Path) -> None:
    """Boundary defense: migrate_one re-validates the name."""
    flat = _write_flat(tmp_path, "agents", "foo", b"v1\n")
    installed_at = _add_lock_entry(tmp_path, "agents", "foo")
    epoch = datetime.fromisoformat(installed_at).timestamp()
    os.utime(flat, (epoch, epoch))

    rows = classify_migrate(tmp_path)
    bad_row = MigrateRow(
        asset_type=rows[0].asset_type,
        name="../etc/passwd",
        flat_path=rows[0].flat_path,
        dir_path=rows[0].dir_path,
        flat_exists=rows[0].flat_exists,
        dir_exists=rows[0].dir_exists,
        has_lock_entry=rows[0].has_lock_entry,
        flat_dirty=rows[0].flat_dirty,
        state=rows[0].state,
        reason=rows[0].reason,
    )
    with pytest.raises(InvalidNameError):
        migrate_one(tmp_path, bad_row, force=False)


def test_migrate_one_path_outside_root_returns_error(tmp_path: Path) -> None:
    """Containment check: paths escaping install root are refused."""
    flat = _write_flat(tmp_path, "agents", "foo", b"v1\n")
    installed_at = _add_lock_entry(tmp_path, "agents", "foo")
    epoch = datetime.fromisoformat(installed_at).timestamp()
    os.utime(flat, (epoch, epoch))

    rows = classify_migrate(tmp_path)
    escaping_row = MigrateRow(
        asset_type=rows[0].asset_type,
        name=rows[0].name,
        flat_path=tmp_path.parent / "evil.md",  # outside .memtomem/agents
        dir_path=tmp_path.parent / "evil",
        flat_exists=False,
        dir_exists=False,
        has_lock_entry=True,
        flat_dirty=False,
        state="migrate",
        reason="escaping",
    )
    result = migrate_one(tmp_path, escaping_row, force=False)
    assert result.ok is False
    assert "escapes" in (result.error or "")


def test_migrate_one_noop_no_writes(tmp_path: Path) -> None:
    _write_dir(tmp_path, "agents", "foo", b"v1\n")
    _add_lock_entry(tmp_path, "agents", "foo")

    rows = classify_migrate(tmp_path)
    assert rows[0].state == "noop"
    pre_lockfile = (tmp_path / ".memtomem" / "lock.json").read_bytes()
    result = migrate_one(tmp_path, rows[0], force=False)

    assert result.ok is True
    # Lockfile unchanged (no rewrite).
    assert (tmp_path / ".memtomem" / "lock.json").read_bytes() == pre_lockfile


# ── CLI integration ──────────────────────────────────────────────────────


def _invoke(args: list[str], project: Path) -> object:
    runner = CliRunner()
    return runner.invoke(context_group, args, catch_exceptions=False, env={"PWD": str(project)})


def test_cli_dry_run_default_no_writes(tmp_path: Path, monkeypatch) -> None:
    flat = _write_flat(tmp_path, "agents", "foo", b"v1\n")
    installed_at = _add_lock_entry(tmp_path, "agents", "foo")
    epoch = datetime.fromisoformat(installed_at).timestamp()
    os.utime(flat, (epoch, epoch))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    runner = CliRunner()
    result = runner.invoke(context_group, ["migrate"])

    assert result.exit_code == 0, result.output
    assert "flat → dir" in result.output
    assert "Run with --apply to execute." in result.output
    # Filesystem unchanged: the flat file still exists.
    assert flat.exists()
    assert not (tmp_path / ".memtomem" / "agents" / "foo" / "agent.md").exists()


def test_cli_apply_yes_migrates(tmp_path: Path, monkeypatch) -> None:
    flat = _write_flat(tmp_path, "agents", "foo", b"v1\n")
    installed_at = _add_lock_entry(tmp_path, "agents", "foo")
    epoch = datetime.fromisoformat(installed_at).timestamp()
    os.utime(flat, (epoch, epoch))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    runner = CliRunner()
    result = runner.invoke(context_group, ["migrate", "--apply", "--yes"])

    assert result.exit_code == 0, result.output
    # POSITIVE
    assert (tmp_path / ".memtomem" / "agents" / "foo" / "agent.md").is_file()
    # NEGATIVE
    assert not flat.exists()


def test_cli_apply_refuses_dirty_no_force(tmp_path: Path, monkeypatch) -> None:
    flat = _write_flat(tmp_path, "agents", "foo", b"v1\n")
    _add_lock_entry(tmp_path, "agents", "foo")
    _bump_mtime(flat)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    runner = CliRunner()
    result = runner.invoke(context_group, ["migrate", "--apply", "--yes"])

    assert result.exit_code == 1, result.output
    assert "--force" in result.output
    assert flat.exists()


def test_cli_apply_force_migrates_dirty_with_bak(tmp_path: Path, monkeypatch) -> None:
    flat = _write_flat(tmp_path, "agents", "foo", b"user-edit\n")
    _add_lock_entry(tmp_path, "agents", "foo")
    _bump_mtime(flat)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    runner = CliRunner()
    result = runner.invoke(context_group, ["migrate", "--apply", "--yes", "--force"])

    assert result.exit_code == 0, result.output
    bak = tmp_path / ".memtomem" / "agents" / "foo.md.bak"
    assert bak.is_file()
    assert bak.read_bytes() == b"user-edit\n"
    target = tmp_path / ".memtomem" / "agents" / "foo" / "agent.md"
    assert target.is_file()
    assert not flat.exists()


def test_cli_skills_explicit_exits_zero(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    runner = CliRunner()
    result = runner.invoke(context_group, ["migrate", "skills"])

    assert result.exit_code == 0, result.output
    assert "always directory layout" in result.output


def test_cli_skills_with_name_exits_zero(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    runner = CliRunner()
    result = runner.invoke(context_group, ["migrate", "skills", "any"])

    assert result.exit_code == 0
    assert "always directory layout" in result.output


def test_cli_force_without_apply_usage_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    runner = CliRunner()
    result = runner.invoke(context_group, ["migrate", "--force"])

    assert result.exit_code != 0
    assert "only valid with --apply" in result.output


def test_cli_yes_force_prints_warning(tmp_path: Path, monkeypatch) -> None:
    flat = _write_flat(tmp_path, "agents", "foo", b"v1\n")
    _add_lock_entry(tmp_path, "agents", "foo")
    _bump_mtime(flat)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    runner = CliRunner()
    result = runner.invoke(context_group, ["migrate", "--apply", "--yes", "--force"])

    assert result.exit_code == 0
    # Click 8.2 mixes stderr into result.output by default.
    assert "WARNING" in result.output


def test_cli_empty_no_flat_assets(tmp_path: Path, monkeypatch) -> None:
    """Empty project tree → exit 0 with informational message."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    runner = CliRunner()
    result = runner.invoke(context_group, ["migrate"])

    assert result.exit_code == 0
    assert "No flat-layout assets to migrate." in result.output


def test_cli_batch_mixes_agents_and_commands(tmp_path: Path, monkeypatch) -> None:
    flat_a = _write_flat(tmp_path, "agents", "foo", b"a\n")
    flat_c = _write_flat(tmp_path, "commands", "build", b"c\n")
    installed_a = _add_lock_entry(tmp_path, "agents", "foo")
    installed_c = _add_lock_entry(tmp_path, "commands", "build")
    epoch_a = datetime.fromisoformat(installed_a).timestamp()
    epoch_c = datetime.fromisoformat(installed_c).timestamp()
    os.utime(flat_a, (epoch_a, epoch_a))
    os.utime(flat_c, (epoch_c, epoch_c))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    runner = CliRunner()
    result = runner.invoke(context_group, ["migrate", "--apply", "--yes"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".memtomem" / "agents" / "foo" / "agent.md").is_file()
    assert (tmp_path / ".memtomem" / "commands" / "build" / "command.md").is_file()
    assert not flat_a.exists()
    assert not flat_c.exists()


def test_cli_targeted_missing_asset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    runner = CliRunner()
    result = runner.invoke(context_group, ["migrate", "agents", "missing"])

    assert result.exit_code == 0
    assert "No matching asset" in result.output


# ── corrupt lockfile + race + adjacent-files defensive coverage ──────────


def test_classify_lockfile_entry_missing_installed_at(tmp_path: Path) -> None:
    """Corrupt lockfile entry (no installed_at) is treated as never_installed.

    Mirrors ``dirty.is_asset_dirty`` semantics: a malformed entry must
    NOT silently let migrate proceed with no dirty check. The flat file
    falls through to ``skip_manual`` (manual provenance) instead.
    """
    import json

    _write_flat(tmp_path, "agents", "foo", b"v1\n")
    # Hand-craft a lockfile entry without ``installed_at`` — bypasses
    # ``Lockfile.upsert_entry`` which would have written one.
    lock_path = tmp_path / ".memtomem" / "lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "version": 1,
                "agents": {"foo": {"wiki_commit": "0" * 40}},
            }
        )
    )

    rows = classify_migrate(tmp_path)

    assert len(rows) == 1
    # No installed_at → entry treated as missing → flat falls into manual
    assert rows[0].state == "skip_manual"
    assert rows[0].has_lock_entry is False


def test_migrate_one_target_appears_mid_execution(tmp_path: Path) -> None:
    """Race between classify and execute: target_file appears, refuse to overwrite.

    Builds a clean classify result (state=migrate, dir absent) and then
    materializes ``dir/agent.md`` before calling ``migrate_one``. The
    execute path must surface the race rather than silently overwriting
    the new bytes.
    """
    flat = _write_flat(tmp_path, "agents", "foo", b"flat-bytes\n")
    installed_at = _add_lock_entry(tmp_path, "agents", "foo")
    epoch = datetime.fromisoformat(installed_at).timestamp()
    os.utime(flat, (epoch, epoch))

    rows = classify_migrate(tmp_path)
    assert rows[0].state == "migrate"

    # Simulate an external writer creating the target between classify
    # and execute.
    target_dir = tmp_path / ".memtomem" / "agents" / "foo"
    target_dir.mkdir(parents=True)
    target_file = target_dir / "agent.md"
    target_file.write_bytes(b"raced bytes\n")

    result = migrate_one(tmp_path, rows[0], force=False)

    assert result.ok is False
    assert "appeared after classify" in (result.error or "")
    # Raced bytes preserved (NOT overwritten by flat content).
    assert target_file.read_bytes() == b"raced bytes\n"
    # Flat preserved too (operation atomic — either both move or neither).
    assert flat.read_bytes() == b"flat-bytes\n"


def test_migrate_one_keeps_unrelated_dir_contents(tmp_path: Path) -> None:
    """Empty target dir with unrelated siblings is fine; non-target files survive.

    Edge case: ``target_dir`` already exists (e.g. partial install left
    ``scripts/`` or other extras) but does not contain ``agent.md`` /
    ``command.md``. Migration should proceed and leave the unrelated
    files untouched — only the asset manifest file is created.
    """
    flat = _write_flat(tmp_path, "agents", "foo", b"agent body\n")
    installed_at = _add_lock_entry(tmp_path, "agents", "foo")
    epoch = datetime.fromisoformat(installed_at).timestamp()
    os.utime(flat, (epoch, epoch))

    # Pre-create the dir with a sibling file (simulates a previous
    # partial install / user-created scaffolding).
    target_dir = tmp_path / ".memtomem" / "agents" / "foo"
    target_dir.mkdir(parents=True)
    sibling = target_dir / "scripts" / "helper.sh"
    sibling.parent.mkdir(parents=True)
    sibling.write_bytes(b"#!/bin/sh\necho hi\n")
    pre_sibling_bytes = sibling.read_bytes()
    pre_sibling_mtime = sibling.stat().st_mtime

    rows = classify_migrate(tmp_path)
    assert rows[0].state == "migrate"
    result = migrate_one(tmp_path, rows[0], force=False)

    assert result.ok is True
    # Asset manifest landed
    assert (target_dir / "agent.md").read_bytes() == b"agent body\n"
    # Unrelated sibling untouched
    assert sibling.read_bytes() == pre_sibling_bytes
    assert sibling.stat().st_mtime == pre_sibling_mtime
    # Flat removed
    assert not flat.exists()


# ── PR-E4: scope-tier migration (17-row smoke matrix) ────────────────


_MANIFEST_NAME = {"agents": "agent.md", "commands": "command.md", "skills": "SKILL.md"}
_RUNTIME_REL = {
    "agents": {
        "claude": ".claude/agents",
        "gemini": ".gemini/agents",
        "codex": ".codex/agents",
    },
    "commands": {
        "claude": ".claude/commands",
        "gemini": ".gemini/commands",
        # Codex has no project-tier commands fan-out (RUNTIME_FANOUT_TABLE
        # returns NO_FANOUT for project_shared/local), but the key exists
        # so the user-tier path can still seed for the codex regression.
        "codex": ".codex/prompts",
    },
    "skills": {
        "claude": ".claude/skills",
        "gemini": ".gemini/skills",
        # Codex skills live under the vendor-neutral Agent Skills path,
        # kimi under .kimi/ — mirrored from RUNTIME_FANOUT_TABLE so the
        # divergence-guard tests can exercise non-claude skill runtimes.
        "codex": ".agents/skills",
        "kimi": ".kimi/skills",
    },
}
_AGENT_BODY_CLEAN = "---\nname: foo\ndescription: a clean test agent\n---\n\nhello world\n"
_COMMAND_BODY_CLEAN = "---\nname: foo\ndescription: a clean test command\n---\n\nhello $ARGUMENTS\n"
_SKILL_BODY_CLEAN = "---\nname: foo\ndescription: a clean test skill\n---\n\nhello\n"
_BODY_CLEAN = {
    "agents": _AGENT_BODY_CLEAN,
    "commands": _COMMAND_BODY_CLEAN,
    "skills": _SKILL_BODY_CLEAN,
}
_SECRET_LITERAL = "AKIA1234567890ABCDEF"  # AWS-key shape — caught by privacy.enforce_write_guard
_AGENT_BODY_SECRET = "---\nname: foo\ndescription: leaks\n---\n\napi_key=" + _SECRET_LITERAL + "\n"


@pytest.fixture
def scope_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Project root + monkeypatched HOME for cross-scope migration tests.

    Layout:

    - ``project_root`` = ``tmp_path / "proj"`` (with ``.git`` so the CLI's
      ``_find_project_root`` walker picks it up).
    - ``user_home`` = ``tmp_path / "home"`` — both ``HOME`` and
      ``USERPROFILE`` are monkeypatched to this path so
      ``canonical_artifact_dir(scope="user")`` and the user-tier runtime
      fan-out (``~/.claude/...``) resolve under it
      (``feedback_path_home_cross_platform.md``).
    - cwd is set to ``project_root`` so the CLI walker terminates there.

    Returns a dict the test functions index for the four useful paths.
    """
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    user_home = tmp_path / "home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.chdir(project_root)
    return {"project_root": project_root, "user_home": user_home}


def _canonical_root_for(layout: dict[str, Path], kind: str, scope: str) -> Path:
    """Mirror ``canonical_artifact_dir`` using the fixture paths."""
    if scope == "user":
        return layout["user_home"] / ".memtomem" / kind
    if scope == "project_shared":
        return layout["project_root"] / ".memtomem" / kind
    if scope == "project_local":
        return layout["project_root"] / ".memtomem" / f"{kind}.local"
    raise ValueError(f"unknown scope: {scope}")


def _write_canonical_dir(
    layout: dict[str, Path], kind: str, scope: str, name: str, body: str
) -> Path:
    """Write a dir-layout canonical artifact and return the manifest path."""
    root = _canonical_root_for(layout, kind, scope)
    manifest_dir = root / name
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = manifest_dir / _MANIFEST_NAME[kind]
    manifest.write_text(body, encoding="utf-8")
    return manifest


# Per-(kind, runtime) file suffix for non-skill runtime artifacts.
# Mirrors the production table in
# ``memtomem.context.migrate._NON_SKILL_FANOUT_SUFFIX`` so the test
# helpers seed the same on-disk shape the production generators write.
# Parity is locked by ``test_e4_runtime_suffix_parity_with_generators``.
_RUNTIME_SUFFIX: dict[str, dict[str, str]] = {
    "agents": {"claude": ".md", "gemini": ".md", "codex": ".toml"},
    "commands": {"claude": ".md", "gemini": ".toml", "codex": ".md"},
}


def _runtime_fanout_path(
    layout: dict[str, Path], kind: str, runtime: str, scope: str, name: str
) -> Path:
    """Compute the runtime fan-out file/dir path for a given (kind, runtime, scope, name)."""
    rel = _RUNTIME_REL[kind][runtime]
    if scope == "user":
        base = layout["user_home"] / rel
    elif scope == "project_shared":
        base = layout["project_root"] / rel
    else:
        raise ValueError(f"runtime fan-out not defined for scope={scope!r}")
    if kind == "skills":
        return base / name
    suffix = _RUNTIME_SUFFIX[kind].get(runtime, ".md")
    return base / f"{name}{suffix}"


def _seed_runtime_fanout(
    layout: dict[str, Path],
    kind: str,
    scope: str,
    name: str,
    body: str,
    *,
    runtimes: tuple[str, ...] = ("claude", "gemini"),
) -> list[Path]:
    """Pre-seed runtime fan-out targets so the migrate cleanup path has something to remove."""
    seeded: list[Path] = []
    for runtime in runtimes:
        target = _runtime_fanout_path(layout, kind, runtime, scope, name)
        if kind == "skills":
            target.mkdir(parents=True, exist_ok=True)
            (target / _MANIFEST_NAME[kind]).write_text(body, encoding="utf-8")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        seeded.append(target)
    return seeded


def _invoke_migrate(args: list[str]) -> object:
    """Click runner shorthand. ``catch_exceptions=False`` lets test failures surface."""
    return CliRunner().invoke(context_group, args, catch_exceptions=False)


def _migrate_args(
    kind: str,
    name: str,
    *,
    from_scope: str | None,
    to_scope: str,
    apply_: bool = True,
    confirm_project_shared: bool = False,
    yes: bool = True,
) -> list[str]:
    args: list[str] = ["migrate", kind, name, "--to", to_scope]
    if from_scope is not None:
        args.extend(["--from", from_scope])
    if apply_:
        args.append("--apply")
    if confirm_project_shared:
        args.append("--confirm-project-shared")
    if yes:
        args.append("--yes")
    return args


# ── Rows 1–10: per-(kind, transition) basic moves ────────────────────


def test_e4_row1_agents_user_to_project_shared_clean(scope_layout):
    """Row 1: agents user→project_shared clean — canonical at dst, src removed."""
    src = _write_canonical_dir(scope_layout, "agents", "user", "foo", _AGENT_BODY_CLEAN)

    result = _invoke_migrate(
        _migrate_args(
            "agents",
            "foo",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert result.exit_code == 0, result.output

    dst = _canonical_root_for(scope_layout, "agents", "project_shared") / "foo" / "agent.md"
    assert dst.is_file()
    assert dst.read_text(encoding="utf-8") == _AGENT_BODY_CLEAN
    assert not src.exists()
    assert not src.parent.exists()


def test_e4_row2_agents_user_to_project_shared_secret_blocks(scope_layout):
    """Row 2: secret on the wire to project_shared — Gate A raises, src untouched."""
    src = _write_canonical_dir(scope_layout, "agents", "user", "foo", _AGENT_BODY_SECRET)

    result = _invoke_migrate(
        _migrate_args(
            "agents",
            "foo",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert result.exit_code != 0, result.output
    assert "Gate A" in result.output
    # POSITIVE: src restored
    assert src.is_file()
    assert src.read_text(encoding="utf-8") == _AGENT_BODY_SECRET
    # NEGATIVE: dst absent
    dst_root = _canonical_root_for(scope_layout, "agents", "project_shared")
    assert not (dst_root / "foo").exists()
    # Staging cleaned (no .migrate-foo-* leftover under dst.parent)
    assert not list(dst_root.glob(".migrate-foo-*.tmp"))


def test_e4_row3_agents_user_to_project_local_clean(scope_layout):
    """Row 3: agents user→project_local clean — canonical at draft tier, src removed."""
    src = _write_canonical_dir(scope_layout, "agents", "user", "foo", _AGENT_BODY_CLEAN)

    result = _invoke_migrate(
        _migrate_args("agents", "foo", from_scope="user", to_scope="project_local")
    )
    assert result.exit_code == 0, result.output

    dst = _canonical_root_for(scope_layout, "agents", "project_local") / "foo" / "agent.md"
    assert dst.is_file()
    assert not src.exists()


def test_e4_row4_agents_project_shared_to_user_clean(scope_layout):
    """Row 4: agents project_shared→user clean — canonical at user, src removed.

    Also pins runtime fan-out cleanup at the source scope: a stale
    ``<proj>/.claude/agents/foo.md`` seeded before migrate is removed
    afterward.
    """
    src = _write_canonical_dir(scope_layout, "agents", "project_shared", "foo", _AGENT_BODY_CLEAN)
    seeded = _seed_runtime_fanout(
        scope_layout, "agents", "project_shared", "foo", _AGENT_BODY_CLEAN
    )

    result = _invoke_migrate(
        _migrate_args("agents", "foo", from_scope="project_shared", to_scope="user")
    )
    assert result.exit_code == 0, result.output

    dst = _canonical_root_for(scope_layout, "agents", "user") / "foo" / "agent.md"
    assert dst.is_file()
    assert not src.exists()
    # Stale fan-out removed
    for path in seeded:
        assert not path.exists(), f"expected stale fan-out cleaned: {path}"


def test_e4_row5_agents_project_local_to_project_shared_clean(scope_layout):
    """Row 5: agents project_local→project_shared clean — promote draft to shared."""
    src = _write_canonical_dir(scope_layout, "agents", "project_local", "foo", _AGENT_BODY_CLEAN)

    result = _invoke_migrate(
        _migrate_args(
            "agents",
            "foo",
            from_scope="project_local",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert result.exit_code == 0, result.output
    dst = _canonical_root_for(scope_layout, "agents", "project_shared") / "foo" / "agent.md"
    assert dst.is_file()
    assert not src.exists()


def test_e4_row6_agents_project_shared_to_project_local_clean(scope_layout):
    """Row 6: agents project_shared→project_local — demote, fan-out dropped."""
    src = _write_canonical_dir(scope_layout, "agents", "project_shared", "foo", _AGENT_BODY_CLEAN)
    seeded = _seed_runtime_fanout(
        scope_layout, "agents", "project_shared", "foo", _AGENT_BODY_CLEAN
    )

    result = _invoke_migrate(
        _migrate_args("agents", "foo", from_scope="project_shared", to_scope="project_local")
    )
    assert result.exit_code == 0, result.output
    dst = _canonical_root_for(scope_layout, "agents", "project_local") / "foo" / "agent.md"
    assert dst.is_file()
    assert not src.exists()
    for path in seeded:
        assert not path.exists(), f"expected stale fan-out cleaned on demote: {path}"


def test_e4_row7_commands_user_to_project_shared_clean(scope_layout):
    """Row 7: parallel of row 1 for commands."""
    src = _write_canonical_dir(scope_layout, "commands", "user", "build", _COMMAND_BODY_CLEAN)

    result = _invoke_migrate(
        _migrate_args(
            "commands",
            "build",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert result.exit_code == 0, result.output
    dst = _canonical_root_for(scope_layout, "commands", "project_shared") / "build" / "command.md"
    assert dst.is_file()
    assert not src.exists()


def test_e4_row8_commands_project_local_to_user_clean(scope_layout):
    """Row 8: commands project_local→user clean."""
    src = _write_canonical_dir(
        scope_layout, "commands", "project_local", "build", _COMMAND_BODY_CLEAN
    )

    result = _invoke_migrate(
        _migrate_args("commands", "build", from_scope="project_local", to_scope="user")
    )
    assert result.exit_code == 0, result.output
    dst = _canonical_root_for(scope_layout, "commands", "user") / "build" / "command.md"
    assert dst.is_file()
    assert not src.exists()


def test_e4_row9_skills_user_to_project_shared_clean(scope_layout):
    """Row 9: skills user→project_shared — canonical moves; user-tier fan-out cleaned.

    Pre-seeds ``~/.claude/skills/foo/`` (user-tier fan-out per ADR-0011
    runtime table) and verifies it is removed after migrate. ADR-0011
    correction: skills `project_shared` IS a fan-out target (only
    `project_local` is NO_FANOUT) — see plan review feedback.
    """
    src = _write_canonical_dir(scope_layout, "skills", "user", "foo", _SKILL_BODY_CLEAN)
    seeded = _seed_runtime_fanout(scope_layout, "skills", "user", "foo", _SKILL_BODY_CLEAN)

    result = _invoke_migrate(
        _migrate_args(
            "skills",
            "foo",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert result.exit_code == 0, result.output

    dst = _canonical_root_for(scope_layout, "skills", "project_shared") / "foo" / "SKILL.md"
    assert dst.is_file()
    assert not src.exists()
    # User-tier fan-out cleaned (rmtree-d skill dir).
    for path in seeded:
        assert not path.exists(), f"expected user-tier skill fan-out cleaned: {path}"


def test_e4_row10_skills_project_shared_to_project_local_clean(scope_layout):
    """Row 10: skills project_shared→project_local — fan-out drops (project_local NO_FANOUT).

    Project_shared skills DO fan out (``<proj>/.claude/skills/foo/``);
    project_local skills do NOT (ADR-0011 §6 / §3). The demote must
    remove the project-shared fan-out so the runtime stops seeing the
    skill.
    """
    src = _write_canonical_dir(scope_layout, "skills", "project_shared", "foo", _SKILL_BODY_CLEAN)
    seeded = _seed_runtime_fanout(
        scope_layout, "skills", "project_shared", "foo", _SKILL_BODY_CLEAN
    )

    result = _invoke_migrate(
        _migrate_args(
            "skills",
            "foo",
            from_scope="project_shared",
            to_scope="project_local",
        )
    )
    assert result.exit_code == 0, result.output

    dst = _canonical_root_for(scope_layout, "skills", "project_local") / "foo" / "SKILL.md"
    assert dst.is_file()
    assert not src.exists()
    for path in seeded:
        assert not path.exists(), (
            f"expected project_shared skills fan-out cleaned on demote: {path}"
        )


# ── Rows 11–15: edge cases ───────────────────────────────────────────


def test_e4_row11_exdev_fallback_copytree(scope_layout, monkeypatch):
    """Row 11: EXDEV — first ``os.rename`` raises EXDEV; staging falls back to copytree.

    Monkeypatches ``os.rename`` once so the ``src → staging`` step
    (the only ``os.rename`` call inside ``migrate_scope`` before
    ``_promote_move``) hits EXDEV. The promote-side ``os.replace`` is
    a different function and therefore unaffected.
    """
    import os as os_mod

    src = _write_canonical_dir(scope_layout, "agents", "user", "foo", _AGENT_BODY_CLEAN)
    real_rename = os_mod.rename
    raised: dict[str, bool] = {"once": False}

    def fake_rename(a, b):
        if not raised["once"]:
            raised["once"] = True
            import errno as _errno

            raise OSError(_errno.EXDEV, "Cross-device link", str(a))
        return real_rename(a, b)

    monkeypatch.setattr("memtomem.context.migrate.os.rename", fake_rename)

    result = _invoke_migrate(
        _migrate_args(
            "agents",
            "foo",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert result.exit_code == 0, result.output
    assert raised["once"], "EXDEV path should have triggered"

    dst = _canonical_root_for(scope_layout, "agents", "project_shared") / "foo" / "agent.md"
    assert dst.is_file()
    assert dst.read_text(encoding="utf-8") == _AGENT_BODY_CLEAN
    # Source cleaned up after EXDEV-fallback copy + promote.
    assert not src.exists()
    assert not src.parent.exists()


def test_e4_row12_idempotent_after_migrate(scope_layout):
    """Row 12: re-running migrate after a successful move is a clear no-op error.

    The "concurrent migrate, same src" thread-race shape collapses to
    "second invocation sees src gone" once the first invocation
    completes. The lock-order test in
    ``test_context_migrate_lock_order.py`` covers true concurrency.
    """
    _write_canonical_dir(scope_layout, "agents", "user", "foo", _AGENT_BODY_CLEAN)
    first = _invoke_migrate(
        _migrate_args(
            "agents",
            "foo",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert first.exit_code == 0, first.output

    second = _invoke_migrate(
        _migrate_args(
            "agents",
            "foo",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert second.exit_code != 0, second.output
    assert "not found at scope='user'" in second.output


def test_e4_row13_inverse_migrate_lock_order_pin(scope_layout):
    """Row 13: deterministic lock-acquire order — sorted by ``str(lock_path)``.

    Asserts the contract directly via ``_acquire_pair_lock`` instead of
    racing two threads (which is harder to make deterministic). The
    threaded deadlock-freedom test lives in
    ``test_context_migrate_lock_order.py``.
    """
    from memtomem.context._atomic import _lock_path_for
    from memtomem.context.migrate import _acquire_pair_lock

    src_dir = _canonical_root_for(scope_layout, "agents", "user") / "foo"
    dst_dir = _canonical_root_for(scope_layout, "agents", "project_shared") / "foo"
    src_dir.parent.mkdir(parents=True, exist_ok=True)
    dst_dir.parent.mkdir(parents=True, exist_ok=True)

    expected_first = min(_lock_path_for(src_dir), _lock_path_for(dst_dir), key=str)

    # Smoke: the helper acquires both locks without deadlock and the
    # sorted order is the documented contract (we do not introspect the
    # internal sequence — taking both locks is sufficient functional
    # evidence; the dedicated lock-order test asserts the order).
    with _acquire_pair_lock(src_dir, dst_dir):
        assert expected_first.parent.is_dir()  # lock parent exists


def test_e4_row14_dry_run_no_mutation(scope_layout):
    """Row 14: dry-run — plan reported, no disk mutation."""
    src = _write_canonical_dir(scope_layout, "agents", "user", "foo", _AGENT_BODY_CLEAN)

    result = _invoke_migrate(
        _migrate_args(
            "agents",
            "foo",
            from_scope="user",
            to_scope="project_shared",
            apply_=False,
            yes=False,  # --yes / --force require --apply
            confirm_project_shared=False,
        )
    )
    assert result.exit_code == 0, result.output
    assert "Plan: migrate" in result.output
    assert "Run with --apply" in result.output

    # Filesystem unchanged.
    assert src.is_file()
    dst_root = _canonical_root_for(scope_layout, "agents", "project_shared")
    assert not (dst_root / "foo").exists()
    # No staging tmp leftover.
    assert not list(dst_root.glob(".migrate-foo-*.tmp"))


def test_e4_row15_dst_conflict_always_refuses(scope_layout):
    """Row 15: dst already has same name — refuse, even with --force."""
    src = _write_canonical_dir(scope_layout, "agents", "user", "foo", _AGENT_BODY_CLEAN)
    dst_existing = _write_canonical_dir(
        scope_layout, "agents", "project_shared", "foo", "stale dst body\n"
    )

    # With --force: --force is rejected as a usage error in scope-mode.
    forced = _invoke_migrate(
        [
            "migrate",
            "agents",
            "foo",
            "--from",
            "user",
            "--to",
            "project_shared",
            "--apply",
            "--confirm-project-shared",
            "--yes",
            "--force",
        ]
    )
    assert forced.exit_code != 0, forced.output
    assert "--force does not apply" in forced.output

    # Without --force: refuses with a destination-exists message.
    result = _invoke_migrate(
        _migrate_args(
            "agents",
            "foo",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert result.exit_code != 0, result.output
    assert "destination already exists" in result.output

    # Both sides preserved.
    assert src.is_file()
    assert dst_existing.read_text(encoding="utf-8") == "stale dst body\n"


# ── Rows 16–17: memory cross-link delegate ───────────────────────────


def test_e4_row16_memory_dispatch_delegates_to_memory_migrate(monkeypatch, tmp_path):
    """Row 16: ``mm context migrate memory <src> --from --to`` delegates to memory-migrate.

    Verifies dispatch parity by monkeypatching ``_memory_migrate_run``
    and checking it gets called with the same args the public
    ``mm context memory-migrate`` would build (path, from, to, apply,
    yes, confirm_project_shared).
    """
    src = tmp_path / "rule.md"
    src.write_text("## Rule\n\nharmless body\n", encoding="utf-8")

    captured: dict[str, object] = {}

    async def _fake_run(sources, from_scope, to_scope, apply_, yes, confirm_project_shared):
        captured["sources"] = sources
        captured["from_scope"] = from_scope
        captured["to_scope"] = to_scope
        captured["apply_"] = apply_
        captured["yes"] = yes
        captured["confirm_project_shared"] = confirm_project_shared

    monkeypatch.setattr("memtomem.cli.context_cmd._memory_migrate_run", _fake_run)
    monkeypatch.chdir(tmp_path)

    result = _invoke_migrate(
        [
            "migrate",
            "memory",
            str(src),
            "--from",
            "user",
            "--to",
            "project_shared",
            "--apply",
            "--confirm-project-shared",
        ]
    )
    assert result.exit_code == 0, result.output
    # Issue #886: _memory_migrate_run takes a list (glob input support);
    # dispatch wraps a single path in a one-element list for parity with
    # the public ``mm context memory-migrate`` single-file path.
    assert captured["sources"] == [src.resolve()]
    assert captured["from_scope"] == "user"
    assert captured["to_scope"] == "project_shared"
    assert captured["apply_"] is True
    assert captured["confirm_project_shared"] is True


def test_e4_row17_memory_dispatch_validates_inputs(monkeypatch, tmp_path):
    """Row 17: memory dispatch validation — missing flags / non-existent path / same-scope.

    Compresses three negative cases into one row since they share the
    same dispatch helper:

    1. Missing ``--to`` → UsageError.
    2. Non-existent source path → ClickException.
    3. ``--from`` == ``--to`` → ClickException.
    """
    monkeypatch.chdir(tmp_path)

    # 1. Missing --to
    r1 = _invoke_migrate(["migrate", "memory", "/some/path", "--from", "user"])
    assert r1.exit_code != 0
    assert "--from and --to are both required" in r1.output

    # 2. Non-existent source
    r2 = _invoke_migrate(
        [
            "migrate",
            "memory",
            "/this/does/not/exist.md",
            "--from",
            "user",
            "--to",
            "project_shared",
            "--apply",
            "--confirm-project-shared",
        ]
    )
    assert r2.exit_code != 0
    assert "does not exist" in r2.output

    # 3. --from == --to
    src = tmp_path / "rule.md"
    src.write_text("body\n", encoding="utf-8")
    r3 = _invoke_migrate(
        [
            "migrate",
            "memory",
            str(src),
            "--from",
            "user",
            "--to",
            "user",
            "--apply",
        ]
    )
    assert r3.exit_code != 0
    assert "must differ" in r3.output


# ── PR-E4 review fold: unreadable canonical cannot bypass Gate A ─────


def test_e4_unreadable_canonical_blocks_project_shared_promotion(scope_layout, monkeypatch):
    """Pre-fold this branch passed: ``_stage_move`` renames a chmod-000
    file into staging without reading it, then ``scan_artifact_tree``
    treated the read failure as ``pass`` (conflated with binary), and
    the secret-bearing file got promoted into the git-tracked
    ``project_shared`` tier with Gate A never inspecting it.

    Pin: ``OSError`` on the staging-side read raises a
    ``ClickException``, ``migrate_scope``'s rollback puts src back, and
    the project_shared destination stays empty. Uses a monkeypatched
    ``Path.read_text`` to simulate the unreadable file portably — real
    ``chmod 000`` works on POSIX but is meaningless on Windows, and the
    monkeypatch exercises the same ``OSError`` branch in both.
    """
    src = _write_canonical_dir(scope_layout, "agents", "user", "leak", _AGENT_BODY_SECRET)

    real_read_bytes = Path.read_bytes

    def explode_on_staged_read(self: Path, *args: object, **kwargs: object) -> bytes:
        # The scan walks staging at <dst.parent>/.migrate-leak-<pid>-<rand>.tmp/.
        # Match by name + the staging suffix marker so unrelated reads
        # (CLI bootstrap, config loading) are unaffected.
        if self.name == "agent.md" and ".migrate-leak-" in str(self.parent):
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_bytes(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_bytes", explode_on_staged_read)

    result = _invoke_migrate(
        _migrate_args(
            "agents",
            "leak",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert result.exit_code != 0, result.output
    # Fail-loud message specifics — never echo the secret bytes.
    assert "cannot read" in result.output
    assert _SECRET_LITERAL not in result.output
    # POSITIVE: src restored bytes-identical to the pre-migrate state.
    assert src.is_file()
    assert src.read_text(encoding="utf-8") == _AGENT_BODY_SECRET
    # NEGATIVE: dst absent and no staging tmp left behind.
    dst_root = _canonical_root_for(scope_layout, "agents", "project_shared")
    assert not (dst_root / "leak").exists()
    assert not list(dst_root.glob(".migrate-leak-*.tmp"))


# ── PR-E4 Codex review fold #2: EXDEV + Gate A combined path ─────────


def test_e4_exdev_then_gate_a_blocks_src_untouched(scope_layout, monkeypatch):
    """Codex review #2 — EXDEV-fallback path must roll back cleanly when
    Gate A then blocks. Combines two branches that Row 11 (clean EXDEV)
    and Row 2 (same-FS Gate A block) cover separately:

    1. ``os.rename`` raises EXDEV → ``_stage_move`` falls back to
       ``copytree``, leaving src on disk and staging as a copy
       (``src_consumed=False``).
    2. Gate A scans staging, finds the secret, raises ClickException.
    3. ``except BaseException`` rollback: src already exists (was never
       renamed away), so the rename-back branch is a no-op; staging
       (the copy) gets dropped via rmtree.

    Pin: src is byte-identical to the pre-migrate state, dst absent,
    no staging leftover, exit non-zero.
    """
    import errno as _errno
    import os as os_mod

    src = _write_canonical_dir(scope_layout, "agents", "user", "leak", _AGENT_BODY_SECRET)
    real_rename = os_mod.rename
    raised: dict[str, bool] = {"once": False}

    def fake_rename(a, b):
        # Trigger EXDEV on the FIRST os.rename call (the src→staging
        # step inside _stage_move). Subsequent renames (none expected
        # in this path because Gate A blocks before _promote_move) go
        # through.
        if not raised["once"]:
            raised["once"] = True
            raise OSError(_errno.EXDEV, "Cross-device link", str(a))
        return real_rename(a, b)

    monkeypatch.setattr("memtomem.context.migrate.os.rename", fake_rename)

    result = _invoke_migrate(
        _migrate_args(
            "agents",
            "leak",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert result.exit_code != 0, result.output
    assert "Gate A" in result.output
    assert raised["once"], "EXDEV branch should have triggered"

    # POSITIVE: src untouched (EXDEV path never consumed it).
    assert src.is_file()
    assert src.read_text(encoding="utf-8") == _AGENT_BODY_SECRET
    # NEGATIVE: dst absent + no staging tmp leftover (rollback dropped it).
    dst_root = _canonical_root_for(scope_layout, "agents", "project_shared")
    assert not (dst_root / "leak").exists()
    assert not list(dst_root.glob(".migrate-leak-*.tmp"))


# ── PR-E4 Codex review fold #1: rollback rename-back failure ─────────


def test_e4_rollback_rename_back_failure_preserves_staging(scope_layout, monkeypatch, caplog):
    """Codex review #1 — when the rollback rename-back fails, staging is
    the only surviving copy of the user's bytes; do NOT delete it.

    Setup: clean canonical at user-tier, Gate A would block (secret),
    and ``os.replace`` is monkeypatched so the rollback's staging→src
    rename-back call raises ``OSError``. The first ``os.replace`` in
    the apply path is the rollback (Gate A blocks before
    ``_promote_move`` runs).

    Pin: error logged with the staging path, staging dir is NOT
    deleted, exit non-zero. Pre-fix the cleanup branch unconditionally
    deleted staging → user data lost.
    """
    import logging as _logging
    import os as os_mod

    src = _write_canonical_dir(scope_layout, "agents", "user", "leak", _AGENT_BODY_SECRET)
    real_replace = os_mod.replace
    rename_back_calls: list[tuple[Path, Path]] = []

    def fake_replace(a, b):
        # The first os.replace in this path is the rollback's
        # staging→src rename-back. Trip it once so the rollback hits the
        # OSError branch; subsequent os.replace calls (none expected
        # along this code path) go through.
        if not rename_back_calls:
            rename_back_calls.append((Path(a), Path(b)))
            raise OSError(13, "Permission denied", str(a))
        return real_replace(a, b)

    monkeypatch.setattr("memtomem.context.migrate.os.replace", fake_replace)
    caplog.set_level(_logging.ERROR, logger="memtomem.context.migrate")

    result = _invoke_migrate(
        _migrate_args(
            "agents",
            "leak",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert result.exit_code != 0, result.output
    # The Gate A path raised first; rollback hit the os.replace failure.
    assert rename_back_calls, "rollback rename-back should have been attempted"

    # POSITIVE: ERROR logged pointing at the surviving staging path so
    # the user can recover manually.
    assert any(
        "rename-back failed" in r.getMessage() and ".migrate-leak-" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]

    # POSITIVE: staging dir survives (the only copy of the bytes).
    dst_root = _canonical_root_for(scope_layout, "agents", "project_shared")
    surviving = list(dst_root.glob(".migrate-leak-*.tmp"))
    assert len(surviving) == 1, surviving
    # Bytes inside staging are byte-identical to the original src
    # (it was renamed, not rewritten).
    surviving_manifest = surviving[0] / "agent.md"
    assert surviving_manifest.is_file()
    assert surviving_manifest.read_text(encoding="utf-8") == _AGENT_BODY_SECRET

    # NEGATIVE: src is gone (consumed by the initial os.rename); dst
    # never landed.
    assert not src.exists()
    assert not (dst_root / "leak").exists()


# ── PR-E4 Codex re-review fold: src reappears via external race ──────


def test_e4_rollback_src_reappears_preserves_staging(scope_layout, monkeypatch, caplog):
    """Codex re-review fold — when ``src_path`` reappears during apply
    (an external writer outside our sidecar lock — e.g.,
    ``mm context install`` running in parallel, a user manually
    recreating the canonical), the rollback must NOT silently delete
    staging. The new src bytes might be unrelated to ours; staging is
    the only verified copy of the original.

    Triggered by monkeypatching ``scan_artifact_tree`` in the
    ``transfer`` module (the scope-move orchestration lives there since
    ADR-0023; ``migrate_scope`` delegates) so that BEFORE Gate A would
    block, the racer recreates ``src_path`` with different bytes. Then
    Gate A blocks, rollback enters the ``src_path.exists()`` branch,
    logs ERROR, and preserves staging.

    Pin (Codex re-review):
      * src reappeared bytes are preserved verbatim — rollback does
        not overwrite them.
      * staging directory survives — original bytes recoverable.
      * ERROR log includes the "reappeared" marker + both paths.
    """
    import logging as _logging

    src = _write_canonical_dir(scope_layout, "agents", "user", "leak", _AGENT_BODY_SECRET)

    from memtomem.context import transfer as transfer_mod

    real_scan = transfer_mod.scan_artifact_tree
    racer_bytes = "racer wrote different bytes\n"

    def fake_scan_with_racer(*args, **kwargs):
        # Recreate src with different bytes BEFORE the scan returns.
        # The scan itself runs against staging — Gate A will block on
        # the original secret. By the time rollback runs, src exists
        # again from the racer's write.
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(racer_bytes, encoding="utf-8")
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(transfer_mod, "scan_artifact_tree", fake_scan_with_racer)
    caplog.set_level(_logging.ERROR, logger="memtomem.context.transfer")

    result = _invoke_migrate(
        _migrate_args(
            "agents",
            "leak",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert result.exit_code != 0, result.output

    # POSITIVE: ERROR logged with the "reappeared" marker.
    messages = [r.getMessage() for r in caplog.records]
    assert any("reappeared during apply" in m and ".migrate-leak-" in m for m in messages), messages

    # POSITIVE: racer bytes preserved at src (not overwritten by rollback).
    assert src.is_file()
    assert src.read_text(encoding="utf-8") == racer_bytes

    # POSITIVE: staging dir survives with the ORIGINAL secret-bearing bytes.
    dst_root = _canonical_root_for(scope_layout, "agents", "project_shared")
    surviving = list(dst_root.glob(".migrate-leak-*.tmp"))
    assert len(surviving) == 1, surviving
    surviving_manifest = surviving[0] / "agent.md"
    assert surviving_manifest.is_file()
    assert surviving_manifest.read_text(encoding="utf-8") == _AGENT_BODY_SECRET

    # NEGATIVE: dst never landed.
    assert not (dst_root / "leak").exists()


# ── #895 P2 review #2: per-runtime fan-out suffix cleanup ────────────


def test_e4_runtime_suffix_parity_with_generators():
    """Pin: migrate's cleanup suffix table matches the generator tables.

    A future runtime addition that writes a new file format must update
    both the generator's per-runtime suffix dict AND the cleanup table
    in :mod:`memtomem.context.migrate`. This test fails immediately if
    they drift, preventing a repeat of the Gemini-commands ``.toml``
    leak (#895 P2 review #2) where the cleanup hardcoded ``.md`` and
    silently orphaned ``.gemini/commands/<name>.toml`` after a scope
    move.
    """
    from memtomem.context.agents import _AGENT_RUNTIME_SUFFIX
    from memtomem.context.commands import _COMMAND_RUNTIME_SUFFIX
    from memtomem.context.migrate import _NON_SKILL_FANOUT_SUFFIX

    assert _NON_SKILL_FANOUT_SUFFIX["agents"] == _AGENT_RUNTIME_SUFFIX
    assert _NON_SKILL_FANOUT_SUFFIX["commands"] == _COMMAND_RUNTIME_SUFFIX


def test_e4_gemini_commands_toml_cleanup_on_migrate(scope_layout):
    """#895 P2 review #2: ``mm context migrate commands foo --from
    project_shared --to user`` must remove ``.gemini/commands/foo.toml``,
    not leave it as an orphan after the canonical move.

    Pre-fix, the cleanup probed ``.gemini/commands/foo.md`` (always
    absent because Gemini writes TOML), so the ``.toml`` survived and
    Gemini could still discover/run the moved-away command at the old
    scope.
    """
    src = _write_canonical_dir(
        scope_layout, "commands", "project_shared", "foo", _COMMAND_BODY_CLEAN
    )
    # Seed both runtimes at the source scope — claude with .md (the
    # existing test path), gemini with .toml (the regression target).
    seeded_claude = _seed_runtime_fanout(
        scope_layout,
        "commands",
        "project_shared",
        "foo",
        _COMMAND_BODY_CLEAN,
        runtimes=("claude",),
    )
    seeded_gemini = _seed_runtime_fanout(
        scope_layout,
        "commands",
        "project_shared",
        "foo",
        _COMMAND_BODY_CLEAN,
        runtimes=("gemini",),
    )
    # Sanity: the seed actually placed a ``.toml`` for gemini.
    assert seeded_gemini[0].suffix == ".toml", seeded_gemini

    result = _invoke_migrate(
        _migrate_args("commands", "foo", from_scope="project_shared", to_scope="user")
    )
    assert result.exit_code == 0, result.output

    dst = _canonical_root_for(scope_layout, "commands", "user") / "foo" / "command.md"
    assert dst.is_file()
    assert not src.exists()
    # POSITIVE pins: BOTH runtimes' stale fan-out cleaned, not just claude.
    for path in seeded_claude + seeded_gemini:
        assert not path.exists(), f"expected stale fan-out cleaned: {path}"


def test_e4_migrate_to_project_local_appends_gitignore_marker(scope_layout):
    """#895 P2 review #3: ``--to project_local --apply`` must append the
    project_local block to ``.gitignore`` so the new local-draft tier
    is not visible to ``git status``.

    Pre-fix, only ``mm context init --scope project_local`` appended the
    marker; users who landed on project_local first via migrate ended up
    with ``.memtomem/agents.local/foo/`` tracked by git.
    """
    from memtomem.cli.context_cmd import _GITIGNORE_MARKER, _GITIGNORE_PATTERNS

    src = _write_canonical_dir(scope_layout, "agents", "user", "foo", _AGENT_BODY_CLEAN)
    gi = scope_layout["project_root"] / ".gitignore"
    assert not gi.exists()  # baseline — no marker yet

    result = _invoke_migrate(
        _migrate_args("agents", "foo", from_scope="user", to_scope="project_local")
    )
    assert result.exit_code == 0, result.output

    dst = _canonical_root_for(scope_layout, "agents", "project_local") / "foo" / "agent.md"
    assert dst.is_file()
    assert not src.exists()
    # POSITIVE pins: marker + both glob patterns now on disk.
    text = gi.read_text(encoding="utf-8")
    assert _GITIGNORE_MARKER in text
    for pat in _GITIGNORE_PATTERNS:
        assert pat in text


def test_e4_migrate_to_project_local_gitignore_is_idempotent(scope_layout):
    """Second migrate to project_local with an existing marker must NOT
    duplicate the block. Mirrors the ``mm context init`` idempotency
    guarantee — the marker comment line is the dedup key.
    """
    from memtomem.cli.context_cmd import _GITIGNORE_MARKER

    # Pre-seed the marker as ``init`` would have done.
    gi = scope_layout["project_root"] / ".gitignore"
    gi.write_text(f"# pre-existing user content\n\n{_GITIGNORE_MARKER}\n.memtomem/*.local/\n")
    before = gi.read_text(encoding="utf-8")

    _write_canonical_dir(scope_layout, "agents", "user", "foo", _AGENT_BODY_CLEAN)
    result = _invoke_migrate(
        _migrate_args("agents", "foo", from_scope="user", to_scope="project_local")
    )
    assert result.exit_code == 0, result.output

    after = gi.read_text(encoding="utf-8")
    # Single occurrence of the marker — no duplicate block appended.
    assert after.count(_GITIGNORE_MARKER) == 1
    assert after == before  # byte-identical


def test_e4_migrate_to_project_shared_does_not_touch_gitignore(scope_layout):
    """Negative pin: a migrate landing in ``project_shared`` (the
    git-tracked tier) must NOT append the project_local block. The
    marker semantic is "this scope is gitignored" — appending on the
    wrong scope would mislead future readers of ``.gitignore``.
    """
    _write_canonical_dir(scope_layout, "agents", "user", "foo", _AGENT_BODY_CLEAN)
    gi = scope_layout["project_root"] / ".gitignore"
    assert not gi.exists()

    result = _invoke_migrate(
        _migrate_args(
            "agents",
            "foo",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert result.exit_code == 0, result.output
    # NEGATIVE: .gitignore was never created.
    assert not gi.exists()


def test_e4_migrate_dry_run_does_not_touch_gitignore(scope_layout):
    """Dry-run preview must not mutate ``.gitignore`` either — the
    contract is "no on-disk writes without ``--apply``".
    """
    _write_canonical_dir(scope_layout, "agents", "user", "foo", _AGENT_BODY_CLEAN)
    gi = scope_layout["project_root"] / ".gitignore"

    result = _invoke_migrate(
        _migrate_args(
            "agents",
            "foo",
            from_scope="user",
            to_scope="project_local",
            apply_=False,
            yes=False,  # ``--yes`` is rejected without ``--apply``
        )
    )
    assert result.exit_code == 0, result.output
    assert not gi.exists()


def test_e4_codex_agents_toml_cleanup_on_migrate(scope_layout):
    """Sibling regression: codex agents write ``.toml`` too. The migrate
    cleanup must use the right suffix so a project_shared→user move
    does not leave ``<proj>/.codex/agents/foo.toml`` behind. Adjacent
    bug to the gemini-commands case — same fix table covers both.
    """
    src = _write_canonical_dir(scope_layout, "agents", "project_shared", "foo", _AGENT_BODY_CLEAN)
    seeded_codex = _seed_runtime_fanout(
        scope_layout,
        "agents",
        "project_shared",
        "foo",
        _AGENT_BODY_CLEAN,
        runtimes=("codex",),
    )
    assert seeded_codex[0].suffix == ".toml", seeded_codex

    result = _invoke_migrate(
        _migrate_args("agents", "foo", from_scope="project_shared", to_scope="user")
    )
    assert result.exit_code == 0, result.output

    dst = _canonical_root_for(scope_layout, "agents", "user") / "foo" / "agent.md"
    assert dst.is_file()
    assert not src.exists()
    for path in seeded_codex:
        assert not path.exists(), f"expected stale codex fan-out cleaned: {path}"


# ── #895 P2 review #5: EXDEV cleanup failure must not report success ─


def test_e4_exdev_src_cleanup_failure_raises_partial_error(scope_layout, monkeypatch):
    """#895 P2 review #5: when the EXDEV fallback successfully copies
    src→dst but fails to remove src (e.g. ``shutil.rmtree`` raises
    OSError on the src cleanup), the migrate MUST raise
    ``MigratePartialError`` rather than logging a warning and
    returning ``moved=True``.

    Pre-fix end state on failure: both src and dst canonicals on disk,
    src_scope's runtime fan-out cleaned (it thought the move succeeded),
    so the next ``mm context sync --scope <src_scope>`` recreates fan-out
    at the OLD tier from the stale src. The autodetect sees two
    canonicals and the user has no remediation hint.
    """
    import errno as _errno
    import os as os_mod
    import shutil as shutil_mod

    src = _write_canonical_dir(scope_layout, "agents", "user", "foo", _AGENT_BODY_CLEAN)
    real_rename = os_mod.rename
    rename_state: dict[str, int] = {"calls": 0}

    def fake_rename(a, b):
        # EXDEV on the FIRST os.rename (the src→staging step). The
        # second os.rename (staging→dst) is allowed through so the
        # copy lands at dst.
        rename_state["calls"] += 1
        if rename_state["calls"] == 1:
            raise OSError(_errno.EXDEV, "Cross-device link", str(a))
        return real_rename(a, b)

    real_rmtree = shutil_mod.rmtree

    def fake_rmtree(path, *args, **kwargs):
        # Refuse to remove the EXDEV-leftover src dir. Everything else
        # (staging cleanup paths, runtime fan-out cleanup) still works.
        if str(path) == str(src.parent):
            raise PermissionError(13, "Permission denied", str(path))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("memtomem.context.migrate.os.rename", fake_rename)
    monkeypatch.setattr("memtomem.context.migrate.shutil.rmtree", fake_rmtree)

    result = _invoke_migrate(
        _migrate_args(
            "agents",
            "foo",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )

    # POSITIVE: exit non-zero with a clear partial-failure message.
    assert result.exit_code != 0, result.output
    assert "canonical copied to" in result.output
    assert "failed to remove stale source" in result.output
    assert "Remove" in result.output and "manually" in result.output
    # Recovery hint must steer the user away from the dangerous
    # ``mm context sync --scope <src_scope>`` re-run.
    assert "do NOT run" in result.output

    # POSITIVE: both src and dst exist (the documented bad state).
    # The error is the loud signal — the user must clean src manually.
    assert src.is_file()
    dst = _canonical_root_for(scope_layout, "agents", "project_shared") / "foo" / "agent.md"
    assert dst.is_file()


# ── #895 P2 review #5: project_shared atomicity — no partial fan-out ─


def test_e4_project_shared_blocked_override_leaves_no_partial_fanout_agents(scope_layout):
    """#895 P2 review #5: when ``scope='project_shared'`` and a later
    runtime's vendor override contains a privacy hit, the earlier
    runtimes' writes must NOT have already landed on disk.

    Pre-fix the outer loop wrote claude/foo.md, then gemini's override
    scan raised, leaving partial fan-out. Post-fix the scan runs in
    Phase 1 over every (target, agent) pair before any write, so the
    first block raises with disk untouched.
    """
    # Clean canonical at project_shared.
    _write_canonical_dir(scope_layout, "agents", "project_shared", "foo", _AGENT_BODY_CLEAN)
    # Override for the SECOND runtime in AGENT_GENERATORS iteration order
    # (gemini) carries the secret. Layout per ADR-0008 + override.py:
    # ``.memtomem/agents/foo/overrides/gemini.md``.
    overrides_dir = scope_layout["project_root"] / ".memtomem" / "agents" / "foo" / "overrides"
    overrides_dir.mkdir(parents=True, exist_ok=True)
    (overrides_dir / "gemini.md").write_text(_AGENT_BODY_SECRET, encoding="utf-8")

    # Drive the same sync the CLI invokes. CliRunner indirection isn't
    # required — generate_all_agents is the boundary where the bug lives.
    from memtomem.context.agents import generate_all_agents
    from memtomem.context.privacy_scan import PrivacyBlockedError

    with pytest.raises(PrivacyBlockedError):
        generate_all_agents(scope_layout["project_root"], scope="project_shared")

    # POSITIVE pins: NEITHER runtime's fan-out target exists on disk.
    # Pre-fix, claude/foo.md (first runtime) would be present.
    claude_fanout = scope_layout["project_root"] / ".claude" / "agents" / "foo.md"
    gemini_fanout = scope_layout["project_root"] / ".gemini" / "agents" / "foo.md"
    assert not claude_fanout.exists(), (
        "Phase 1 must catch the blocked override before Phase 2 writes "
        "claude_agents — partial fan-out violates ADR §5 atomicity."
    )
    assert not gemini_fanout.exists()


def test_e4_project_shared_blocked_override_leaves_no_partial_fanout_commands(scope_layout):
    """Sibling of the agents test — same atomicity contract for commands.
    Gemini commands ship as TOML so the override file uses ``.toml``
    (see ``OVERRIDE_FORMATS[("commands", "gemini")]``).
    """
    _write_canonical_dir(scope_layout, "commands", "project_shared", "foo", _COMMAND_BODY_CLEAN)
    overrides_dir = scope_layout["project_root"] / ".memtomem" / "commands" / "foo" / "overrides"
    overrides_dir.mkdir(parents=True, exist_ok=True)
    (overrides_dir / "gemini.toml").write_text(
        f'prompt = "leaks {_SECRET_LITERAL}"\ndescription = "leak"\n',
        encoding="utf-8",
    )

    from memtomem.context.commands import generate_all_commands
    from memtomem.context.privacy_scan import PrivacyBlockedError

    with pytest.raises(PrivacyBlockedError):
        generate_all_commands(scope_layout["project_root"], scope="project_shared")

    claude_fanout = scope_layout["project_root"] / ".claude" / "commands" / "foo.md"
    gemini_fanout = scope_layout["project_root"] / ".gemini" / "commands" / "foo.toml"
    assert not claude_fanout.exists(), (
        "Phase 1 must catch the blocked override before Phase 2 writes "
        "claude_commands — partial fan-out violates ADR §5 atomicity."
    )
    assert not gemini_fanout.exists()


# ── #1247 id 7: EXDEV fallback must not dereference symlinks ─────────


def _exdev_once(monkeypatch) -> dict[str, bool]:
    """Make the first ``os.rename`` inside migrate raise EXDEV (Row-11 pattern)."""
    import os as os_mod

    real_rename = os_mod.rename
    raised: dict[str, bool] = {"once": False}

    def fake_rename(a, b):
        if not raised["once"]:
            raised["once"] = True
            import errno as _errno

            raise OSError(_errno.EXDEV, "Cross-device link", str(a))
        return real_rename(a, b)

    monkeypatch.setattr("memtomem.context.migrate.os.rename", fake_rename)
    return raised


@pytest.mark.requires_symlinks
def test_exdev_symlink_in_skill_tree_stays_a_link(scope_layout, monkeypatch, tmp_path):
    """#1247 id 7: cross-FS staging must mirror the same-FS rename semantics.

    Pre-fix, the EXDEV fallback used ``shutil.copytree`` with the stdlib
    default ``symlinks=False``, dereferencing a resolvable symlink and
    materializing its out-of-tree target bytes into staging — and from
    there into the git-shareable ``.memtomem/`` tier — while the same-FS
    rename path moves the link as a link. This violates the package's own
    no-deref mirror contract (``_atomic.copy_tree_atomic``).
    """
    marker = "OUT_OF_TREE_MARKER_1247_ID7"
    out_of_tree = tmp_path / "outside" / "secret-notes.md"
    out_of_tree.parent.mkdir(parents=True)
    out_of_tree.write_text(marker + "\n", encoding="utf-8")

    src_manifest = _write_canonical_dir(scope_layout, "skills", "user", "foo", _SKILL_BODY_CLEAN)
    (src_manifest.parent / "link.md").symlink_to(out_of_tree)

    raised = _exdev_once(monkeypatch)
    result = _invoke_migrate(
        _migrate_args(
            "skills",
            "foo",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert result.exit_code == 0, result.output
    assert raised["once"], "EXDEV path should have triggered"

    dst_dir = _canonical_root_for(scope_layout, "skills", "project_shared") / "foo"
    link = dst_dir / "link.md"
    # POSITIVE: the link survived as a link, still pointing out of tree.
    assert link.is_symlink(), "EXDEV staging dereferenced the symlink"
    assert link.resolve() == out_of_tree.resolve()
    # NEGATIVE: no regular file in the promoted tree carries the target bytes.
    for p in dst_dir.rglob("*"):
        if p.is_file() and not p.is_symlink():
            assert marker not in p.read_text(encoding="utf-8"), (
                f"out-of-tree bytes materialized into {p}"
            )


@pytest.mark.requires_symlinks
def test_exdev_flat_symlink_canonical_stays_a_link(scope_layout, monkeypatch, tmp_path):
    """Flat-artifact sibling of the tree case: ``shutil.copy2`` must not
    follow a symlinked canonical on the EXDEV path (``follow_symlinks=False``
    parity with the rename path, which moves the link itself)."""
    target_body = "---\nname: foo\ndescription: linked agent\n---\n\nlinked body\n"
    out_of_tree = tmp_path / "outside" / "real-agent.md"
    out_of_tree.parent.mkdir(parents=True)
    out_of_tree.write_text(target_body, encoding="utf-8")

    flat_root = _canonical_root_for(scope_layout, "agents", "user")
    flat_root.mkdir(parents=True, exist_ok=True)
    flat = flat_root / "foo.md"
    flat.symlink_to(out_of_tree)

    raised = _exdev_once(monkeypatch)
    result = _invoke_migrate(
        _migrate_args("agents", "foo", from_scope="user", to_scope="project_local")
    )
    assert result.exit_code == 0, result.output
    assert raised["once"], "EXDEV path should have triggered"

    dst = _canonical_root_for(scope_layout, "agents", "project_local") / "foo.md"
    # POSITIVE: the moved flat canonical is still a symlink to the same target.
    assert dst.is_symlink(), "EXDEV copy2 materialized the symlinked flat canonical"
    assert dst.resolve() == out_of_tree.resolve()
    # The out-of-tree target itself is untouched.
    assert out_of_tree.read_text(encoding="utf-8") == target_body
    # EXDEV cleanup removed the src link, not the link target.
    assert not flat.exists()


# ── #1247 id 6: fan-out cleanup divergence guard + dry-run preview ───


def _rendered_fanout_bytes(manifest: Path, kind: str, runtime: str) -> bytes:
    """Exactly what sync would write for this canonical at this runtime."""
    if kind == "agents":
        from memtomem.context.agents import AGENT_GENERATORS, parse_canonical_agent

        item = parse_canonical_agent(manifest, layout="dir")
        content, _ = AGENT_GENERATORS[f"{runtime}_agents"].render(item)
    else:
        from memtomem.context.commands import COMMAND_GENERATORS, parse_canonical_command

        item = parse_canonical_command(manifest, layout="dir")
        content, _ = COMMAND_GENERATORS[f"{runtime}_commands"].render(item)
    return content.encode("utf-8")


def test_fanout_diverged_agent_snapshotted_to_bak(scope_layout):
    """#1247 id 6: a runtime-side edit made after the last sync must survive
    the migrate cleanup as a ``.bak`` sibling instead of being destroyed.

    Pre-fix, ``_remove_runtime_fanout_for`` unlinked by name with no
    content check and no backup — the diverged bytes existed nowhere else.
    """
    src = _write_canonical_dir(scope_layout, "agents", "project_shared", "foo", _AGENT_BODY_CLEAN)
    target = _runtime_fanout_path(scope_layout, "agents", "claude", "project_shared", "foo")
    target.parent.mkdir(parents=True, exist_ok=True)
    diverged = _rendered_fanout_bytes(src, "agents", "claude") + b"\n# local runtime edit\n"
    target.write_bytes(diverged)
    # Sibling negative pin: gemini holds EXACTLY what sync wrote — must be
    # removed clean, with no backup.
    in_sync = _runtime_fanout_path(scope_layout, "agents", "gemini", "project_shared", "foo")
    in_sync.parent.mkdir(parents=True, exist_ok=True)
    in_sync.write_bytes(_rendered_fanout_bytes(src, "agents", "gemini"))

    result = _invoke_migrate(
        _migrate_args("agents", "foo", from_scope="project_shared", to_scope="user")
    )
    assert result.exit_code == 0, result.output

    # POSITIVE: both targets removed; the diverged one left a .bak with
    # the edited bytes.
    assert not target.exists()
    assert not in_sync.exists()
    bak = target.with_name(target.name + ".bak")
    assert bak.is_file(), "diverged fan-out target deleted without a backup"
    assert bak.read_bytes() == diverged
    assert "snapshotted before removal" in result.output
    assert str(bak) in result.output
    # NEGATIVE (pin-and-invert): the in-sync target must NOT leave a .bak.
    assert not in_sync.with_name(in_sync.name + ".bak").exists()


def test_fanout_override_carrying_agent_in_sync_no_bak(scope_layout):
    """A per-vendor override REPLACES the rendered file at sync time
    (ADR-0008 Invariant 4); the divergence check must compare against the
    override bytes — overrides moved with the artifact dir — or every
    override-carrying artifact would false-positive a .bak on migrate."""
    src = _write_canonical_dir(scope_layout, "agents", "project_shared", "foo", _AGENT_BODY_CLEAN)
    override = src.parent / "overrides" / "claude.md"
    override.parent.mkdir(parents=True)
    override_body = b"override body: claude-specific\n"
    override.write_bytes(override_body)
    target = _runtime_fanout_path(scope_layout, "agents", "claude", "project_shared", "foo")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(override_body)

    result = _invoke_migrate(
        _migrate_args("agents", "foo", from_scope="project_shared", to_scope="user")
    )
    assert result.exit_code == 0, result.output

    assert not target.exists()
    # POSITIVE: clean delete — no backup, no divergence warning.
    assert not target.with_name(target.name + ".bak").exists()
    assert "snapshotted before removal" not in result.output


def test_fanout_hand_authored_skill_content_snapshotted(scope_layout):
    """The heaviest id-6 loss path: ``rmtree`` of a skill dir holding files
    sync never wrote. The extra bytes must survive under
    ``<runtime_root>/.bak/<name>/`` — one level down so neither our
    discovery loops (diff/extract: ``<root>/*/SKILL.md``) nor the real
    runtimes can rediscover the stale skill, and extract cannot round-trip
    it back into canonical (#1229's failure mode with a sibling name).
    """
    _write_canonical_dir(scope_layout, "skills", "user", "foo", _SKILL_BODY_CLEAN)
    target = _runtime_fanout_path(scope_layout, "skills", "claude", "user", "foo")
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(_SKILL_BODY_CLEAN, encoding="utf-8")
    notes = "hand-authored runtime-only notes\n"
    (target / "notes.txt").write_text(notes, encoding="utf-8")

    result = _invoke_migrate(
        _migrate_args(
            "skills",
            "foo",
            from_scope="user",
            to_scope="project_shared",
            confirm_project_shared=True,
        )
    )
    assert result.exit_code == 0, result.output

    assert not target.exists(), "stale runtime skill dir must still be removed"
    bak = target.parent / ".bak" / "foo"
    assert (bak / "notes.txt").read_text(encoding="utf-8") == notes
    assert (bak / "SKILL.md").read_text(encoding="utf-8") == _SKILL_BODY_CLEAN
    # The backup parent holds no SKILL.md itself — single-level discovery
    # (diff, extract, real runtimes) cannot see the snapshot as a skill.
    assert not (target.parent / ".bak" / "SKILL.md").exists()


def test_fanout_diverged_kimi_skill_snapshotted(scope_layout):
    """Non-claude skill runtime coverage (Codex design review): the same
    guard must hold for the kimi fan-out tree."""
    _write_canonical_dir(scope_layout, "skills", "user", "foo", _SKILL_BODY_CLEAN)
    target = _runtime_fanout_path(scope_layout, "skills", "kimi", "user", "foo")
    target.mkdir(parents=True)
    edited = _SKILL_BODY_CLEAN + "\nkimi-side local edit\n"
    (target / "SKILL.md").write_text(edited, encoding="utf-8")

    result = _invoke_migrate(
        _migrate_args("skills", "foo", from_scope="user", to_scope="project_local")
    )
    assert result.exit_code == 0, result.output

    assert not target.exists()
    bak = target.parent / ".bak" / "foo"
    assert (bak / "SKILL.md").read_text(encoding="utf-8") == edited


def test_fanout_exact_skill_copy_removed_without_bak(scope_layout):
    """Negative pin (pin-and-invert): a runtime skill tree that byte-matches
    the canonical is removed with NO backup — the .bak valve is for
    diverged content only, not a tombstone for every migrate."""
    _write_canonical_dir(scope_layout, "skills", "user", "foo", _SKILL_BODY_CLEAN)
    target = _runtime_fanout_path(scope_layout, "skills", "claude", "user", "foo")
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(_SKILL_BODY_CLEAN, encoding="utf-8")

    result = _invoke_migrate(
        _migrate_args("skills", "foo", from_scope="user", to_scope="project_local")
    )
    assert result.exit_code == 0, result.output

    assert not target.exists()
    assert not (target.parent / ".bak").exists()
    assert "snapshotted before removal" not in result.output


def test_fanout_foreign_codex_prompt_left_in_place(scope_layout):
    """``~/.codex/prompts`` is a reserved table row with NO generator —
    sync can never have written there, so a hand-authored prompt that
    happens to share the command's name must survive the migrate, and the
    dry-run preview must not claim it as planned cleanup (Codex design
    review: preview/apply parity). Pre-fix, both halves were wrong: the
    file was deleted, invisibly."""
    _write_canonical_dir(scope_layout, "commands", "user", "foo", _COMMAND_BODY_CLEAN)
    prompt = scope_layout["user_home"] / ".codex" / "prompts" / "foo.md"
    prompt.parent.mkdir(parents=True)
    hand_authored = "# my hand-written codex prompt — not memtomem's\n"
    prompt.write_text(hand_authored, encoding="utf-8")

    preview = _invoke_migrate(
        _migrate_args(
            "commands", "foo", from_scope="user", to_scope="project_local", apply_=False, yes=False
        )
    )
    assert preview.exit_code == 0, preview.output
    # NEGATIVE: not in the planned-cleanup preview.
    assert str(prompt) not in preview.output

    result = _invoke_migrate(
        _migrate_args("commands", "foo", from_scope="user", to_scope="project_local")
    )
    assert result.exit_code == 0, result.output
    # POSITIVE: the foreign file survived, byte-identical, no .bak churn.
    assert prompt.read_text(encoding="utf-8") == hand_authored
    assert not prompt.with_name("foo.md.bak").exists()


@pytest.mark.requires_symlinks
def test_fanout_symlink_target_left_in_place(scope_layout, tmp_path):
    """A symlinked runtime target is user hand-routing, not sync output
    (generators only ever ``os.replace`` regular files): never deref,
    back up, or remove it. Pre-fix it was unlinked."""
    src = _write_canonical_dir(scope_layout, "agents", "user", "foo", _AGENT_BODY_CLEAN)
    referent = tmp_path / "outside" / "routed-agent.md"
    referent.parent.mkdir(parents=True)
    referent.write_bytes(_rendered_fanout_bytes(src, "agents", "claude"))
    target = _runtime_fanout_path(scope_layout, "agents", "claude", "user", "foo")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(referent)

    result = _invoke_migrate(
        _migrate_args("agents", "foo", from_scope="user", to_scope="project_local")
    )
    assert result.exit_code == 0, result.output

    assert target.is_symlink(), "symlinked fan-out target must be left in place"
    assert referent.is_file()
    assert str(target) not in result.output


def test_fanout_dry_run_previews_deletion_half(scope_layout):
    """#1247 id 6: the dry-run plan must show the deletion half of the
    move — pre-fix the fan-out paths were only reported AFTER apply."""
    _write_canonical_dir(scope_layout, "agents", "project_shared", "foo", _AGENT_BODY_CLEAN)
    seeded = _seed_runtime_fanout(
        scope_layout, "agents", "project_shared", "foo", _AGENT_BODY_CLEAN
    )

    preview = _invoke_migrate(
        _migrate_args(
            "agents", "foo", from_scope="project_shared", to_scope="user", apply_=False, yes=False
        )
    )
    assert preview.exit_code == 0, preview.output
    assert "will remove 2 stale runtime fan-out target(s)" in preview.output
    for path in seeded:
        assert str(path) in preview.output
        assert path.exists(), "dry-run must not delete anything"


def test_fanout_backup_failure_keeps_file_target(scope_layout, monkeypatch):
    """When the .bak snapshot of a diverged FILE target fails, the target
    must be KEPT — deleting without a backup is exactly the loss the
    guard exists to prevent."""
    src = _write_canonical_dir(scope_layout, "agents", "user", "foo", _AGENT_BODY_CLEAN)
    target = _runtime_fanout_path(scope_layout, "agents", "claude", "user", "foo")
    target.parent.mkdir(parents=True, exist_ok=True)
    diverged = _rendered_fanout_bytes(src, "agents", "claude") + b"\n# edit\n"
    target.write_bytes(diverged)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("memtomem.context.migrate.shutil.copy2", boom)

    result = _invoke_migrate(
        _migrate_args("agents", "foo", from_scope="user", to_scope="project_local")
    )
    assert result.exit_code == 0, result.output  # cleanup stays best-effort

    assert target.read_bytes() == diverged, "target deleted despite failed backup"
    assert not target.with_name(target.name + ".bak").exists()


def test_fanout_backup_failure_keeps_skill_dir(scope_layout, monkeypatch):
    """Skill-tree sibling of the file case (Codex design review): a failed
    ``copytree`` snapshot must keep the whole runtime skill dir — the
    heaviest id-6 loss path is the dir ``rmtree``."""
    _write_canonical_dir(scope_layout, "skills", "user", "foo", _SKILL_BODY_CLEAN)
    target = _runtime_fanout_path(scope_layout, "skills", "claude", "user", "foo")
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(_SKILL_BODY_CLEAN, encoding="utf-8")
    notes = "runtime-only bytes\n"
    (target / "notes.txt").write_text(notes, encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("memtomem.context.migrate.shutil.copytree", boom)

    result = _invoke_migrate(
        _migrate_args("skills", "foo", from_scope="user", to_scope="project_local")
    )
    assert result.exit_code == 0, result.output

    assert (target / "notes.txt").read_text(encoding="utf-8") == notes, (
        "skill dir rmtree'd despite failed backup"
    )
    assert not (target.parent / ".bak" / "foo" / "notes.txt").exists()


# ── adopt_flat_to_dir (ADR-0022 rank 6) ──────────────────────────────────


def test_adopt_flat_to_dir_renames_byte_identical(tmp_path: Path) -> None:
    """A flat canonical is moved into ``<name>/agent.md`` with bytes intact and
    the flat file gone — the single conversion versioning needs."""
    flat = _write_flat(tmp_path, "agents", "foo", b"---\nx: 1\n---\nbody\n")
    dir_path = tmp_path / ".memtomem" / "agents" / "foo"

    new_working = adopt_flat_to_dir("agents", flat, dir_path)

    assert new_working == dir_path / "agent.md"
    assert new_working.read_bytes() == b"---\nx: 1\n---\nbody\n"
    assert not flat.exists()  # the flat sibling is consumed, not duplicated


def test_adopt_flat_to_dir_works_for_commands(tmp_path: Path) -> None:
    flat = _write_flat(tmp_path, "commands", "deploy", b"run\n")
    dir_path = tmp_path / ".memtomem" / "commands" / "deploy"
    new_working = adopt_flat_to_dir("commands", flat, dir_path)
    assert new_working == dir_path / "command.md"
    assert new_working.read_bytes() == b"run\n"
    assert not flat.exists()


def test_adopt_flat_to_dir_succeeds_without_lockfile_entry(tmp_path: Path) -> None:
    """The whole point of rank 6: a web-created flat file (no lockfile entry —
    which ``classify_migrate`` would mark ``skip_manual``) is still adoptable."""
    flat = _write_flat(tmp_path, "agents", "ui-made", b"body\n")
    # No ``_add_lock_entry`` call — classify_migrate would refuse this file.
    rows = classify_migrate(tmp_path, "agents", "ui-made")
    assert rows and rows[0].state == "skip_manual"  # confirms the dead-end

    new_working = adopt_flat_to_dir("agents", flat, tmp_path / ".memtomem" / "agents" / "ui-made")
    assert new_working.is_file()
    # The lockfile is never touched — adopt is provenance-agnostic.
    assert not (tmp_path / ".memtomem" / "lock.json").exists()


def test_adopt_flat_to_dir_missing_flat_raises(tmp_path: Path) -> None:
    (tmp_path / ".memtomem" / "agents").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        adopt_flat_to_dir(
            "agents",
            tmp_path / ".memtomem" / "agents" / "ghost.md",
            tmp_path / ".memtomem" / "agents" / "ghost",
        )


def test_adopt_flat_to_dir_collision_refuses(tmp_path: Path) -> None:
    """flat + dir both present is the user-edit-bearing ``cleanup_flat`` case;
    adopt refuses it (FileExistsError) and leaves both files untouched."""
    flat = _write_flat(tmp_path, "agents", "foo", b"flat\n")
    _write_dir(tmp_path, "agents", "foo", b"dir\n")
    with pytest.raises(FileExistsError):
        adopt_flat_to_dir("agents", flat, tmp_path / ".memtomem" / "agents" / "foo")
    assert flat.read_bytes() == b"flat\n"  # untouched
    assert (tmp_path / ".memtomem" / "agents" / "foo" / "agent.md").read_bytes() == b"dir\n"


def test_adopt_flat_to_dir_unknown_type_raises(tmp_path: Path) -> None:
    flat = _write_flat(tmp_path, "agents", "foo", b"x\n")
    with pytest.raises(ValueError):
        adopt_flat_to_dir("skills", flat, tmp_path / ".memtomem" / "skills" / "foo")


def test_classify_lockfile_entry_unparseable_installed_at(tmp_path: Path) -> None:
    """A malformed installed_at STRING demotes to skip_manual, not a crash
    and not "clean" (#1247 id 1).

    Pre-fix the isinstance-only guard let the string through to
    ``_is_flat_file_dirty``'s unguarded ``fromisoformat`` → ValueError.
    Returning False there instead would be worse — False means clean,
    which would let migrate remove/overwrite a possibly-edited flat file
    on a corrupt entry."""
    import json

    _write_flat(tmp_path, "agents", "foo", b"v1\n")
    lock_path = tmp_path / ".memtomem" / "lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "version": 1,
                "agents": {"foo": {"wiki_commit": "0" * 40, "installed_at": "yesterday"}},
            }
        )
    )

    rows = classify_migrate(tmp_path)

    assert len(rows) == 1
    row = rows[0]
    assert row.state == "skip_manual"
    assert row.has_lock_entry is False
    assert row.flat_dirty is None
