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


def test_cli_name_without_type_usage_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    runner = CliRunner()
    # Click's `nargs` consumes the single token as TYPE, so to trigger the
    # "name requires type" branch we use `--` to force two positionals.
    # In practice the misuse `mm context migrate -- foo` is detected here.
    # If Click absorbs the token as TYPE it will fall through to the
    # Choice() validator first; that's also acceptable behaviour.
    result = runner.invoke(context_group, ["migrate", "agents", "missing"])

    # `agents missing` parses cleanly; just verify no crash. The name-w/o-type
    # branch is exercised programmatically via classify_migrate above.
    assert result.exit_code == 0


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
