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
    "agents": {"claude": ".claude/agents", "gemini": ".gemini/agents"},
    "commands": {"claude": ".claude/commands", "gemini": ".gemini/commands"},
    "skills": {"claude": ".claude/skills", "gemini": ".gemini/skills"},
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
    return base / name if kind == "skills" else base / f"{name}.md"


def _seed_runtime_fanout(
    layout: dict[str, Path], kind: str, scope: str, name: str, body: str
) -> list[Path]:
    """Pre-seed runtime fan-out targets so the migrate cleanup path has something to remove."""
    seeded: list[Path] = []
    for runtime in ("claude", "gemini"):
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

    async def _fake_run(source, from_scope, to_scope, apply_, yes, confirm_project_shared):
        captured["source"] = source
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
    assert captured["source"] == src.resolve()
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
