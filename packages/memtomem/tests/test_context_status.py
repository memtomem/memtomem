"""Tests for ``memtomem.context.status`` and the ``mm context status`` CLI.

Covers PR-D C3 commit 1: read-only inventory + drift classification of
installed wiki assets, no-write invariant, exit-code contract, and the
wiki-absent / corrupt-lockfile graceful-degradation paths.

These tests construct lockfile + dest tree + wiki manually using the
same shared fixtures as ``test_context_update.py`` (``wiki_root``,
``git_identity`` from ``_wiki_fixtures``). Pin reachability tests
exercise the new ``WikiStore.commit_is_reachable`` helper added in
the same commit.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context as context_group
from memtomem.context._atomic import atomic_write_bytes, installed_at_from_dest
from memtomem.context.lockfile import Lockfile
from memtomem.context.status import StatusRow, classify_status
from memtomem.wiki.store import WikiStore


# ── helpers ──────────────────────────────────────────────────────────────


def _initialized_wiki(wiki_root_path: Path) -> WikiStore:
    store = WikiStore.at_default()
    store.init()
    return store


def _seed_wiki_skill(wiki_root_path: Path, name: str, files: dict[str, bytes]) -> str:
    """Add ``skills/<name>/`` to wiki + git commit. Returns the commit SHA."""
    skill_dir = wiki_root_path / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    for relpath, data in files.items():
        target = skill_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", f"add {name}"],
        check=True,
        capture_output=True,
    )
    return WikiStore.at_default().current_commit()


def _modify_wiki_skill(wiki_root_path: Path, name: str, files: dict[str, bytes]) -> str:
    """Modify wiki skill files + commit. Wiki HEAD advances."""
    skill_dir = wiki_root_path / "skills" / name
    for relpath, data in files.items():
        target = skill_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", f"modify {name}"],
        check=True,
        capture_output=True,
    )
    return WikiStore.at_default().current_commit()


def _setup_installed_at_pin(
    project: Path,
    asset_type: str,
    name: str,
    files: dict[str, bytes],
    pin: str,
) -> str:
    """Manually drop bytes + lockfile entry pinned at *pin*. Returns installed_at."""
    dest = project / ".memtomem" / asset_type / name
    dest.mkdir(parents=True)
    for relpath, data in files.items():
        target = dest / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    installed_at = installed_at_from_dest(dest)
    Lockfile.at(project).upsert_entry(asset_type, name, wiki_commit=pin, installed_at=installed_at)
    return installed_at


def _bump_mtime(path: Path, *, seconds_in_future: float = 1.0) -> None:
    import os

    future = datetime.now(timezone.utc).timestamp() + seconds_in_future
    os.utime(path, (future, future))


def _rewrite_wiki_history(wiki_root_path: Path, name: str) -> None:
    """Force-rewrite wiki history so previously-pinned commits become orphan.

    Resets the branch to the initial scaffold commit and recommits with new content,
    so any pre-rewrite SHA is no longer reachable from refs.
    """
    # Soft-reset to root, then drop and re-add the asset to produce a different SHA.
    initial = subprocess.run(
        ["git", "-C", str(wiki_root_path), "rev-list", "--max-parents=0", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "reset", "--hard", initial],
        check=True,
        capture_output=True,
    )
    skill_dir = wiki_root_path / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_bytes(b"# rewritten\n")
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", f"rewrite {name}"],
        check=True,
        capture_output=True,
    )
    # Run gc so the orphaned commit is no longer reachable from any ref. Cat-file
    # would still find it if reflog/loose objects keep it; expire reflog first.
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "reflog", "expire", "--expire=now", "--all"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "gc", "--prune=now", "--quiet"],
        check=True,
        capture_output=True,
    )


# ── pure classifier tests ────────────────────────────────────────────────


def test_empty_lockfile(wiki_root: Path, tmp_path: Path) -> None:
    """No lockfile entries → empty rows, wiki_head still resolved."""
    _initialized_wiki(wiki_root)

    head, rows = classify_status(tmp_path)

    assert head is not None
    assert rows == []


def test_ok_state_pin_at_head(wiki_root: Path, tmp_path: Path) -> None:
    """Dest clean, pin == wiki HEAD → state=ok."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, pin)

    head, rows = classify_status(tmp_path)

    assert head == pin
    assert len(rows) == 1
    assert rows[0].state == "ok"
    assert rows[0].pin_commit == pin
    assert rows[0].dirty_file_count == 0
    assert rows[0].reason is None


def test_behind_state_pin_below_head(wiki_root: Path, tmp_path: Path) -> None:
    """Pin reachable, pin != HEAD, dest clean → state=behind."""
    _initialized_wiki(wiki_root)
    old_pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, old_pin)
    new_head = _modify_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v2\n"})

    head, rows = classify_status(tmp_path)

    assert head == new_head
    assert head != old_pin
    assert len(rows) == 1
    assert rows[0].state == "behind"
    assert rows[0].pin_commit == old_pin


def test_dirty_state_after_local_edit(wiki_root: Path, tmp_path: Path) -> None:
    """Dest mtime > installed_at → state=dirty + count + reason."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, pin)
    edited = tmp_path / ".memtomem" / "skills" / "foo" / "SKILL.md"
    edited.write_bytes(b"manual edit\n")
    _bump_mtime(edited)

    _, rows = classify_status(tmp_path)

    assert len(rows) == 1
    assert rows[0].state == "dirty"
    assert rows[0].dirty_file_count == 1
    assert "modified locally" in (rows[0].reason or "")


def test_missing_state_dest_deleted(wiki_root: Path, tmp_path: Path) -> None:
    """Lockfile entry exists but dest dir deleted → state=missing."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, pin)
    shutil.rmtree(tmp_path / ".memtomem" / "skills" / "foo")

    _, rows = classify_status(tmp_path)

    assert len(rows) == 1
    assert rows[0].state == "missing"
    assert rows[0].reason == "dest missing"


def test_stale_pin_when_wiki_absent(wiki_root: Path, tmp_path: Path) -> None:
    """Wiki absent → wiki_head=None, rows still render, clean rows go stale-pin."""
    # Don't init the wiki.
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, "0" * 40)

    head, rows = classify_status(tmp_path)

    assert head is None
    assert len(rows) == 1
    assert rows[0].state == "stale-pin"
    assert rows[0].reason == "wiki not present"


def test_stale_pin_when_history_rewritten(wiki_root: Path, tmp_path: Path) -> None:
    """Pin not reachable in wiki (force-pushed past) → state=stale-pin."""
    _initialized_wiki(wiki_root)
    orphan_pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, orphan_pin)

    _rewrite_wiki_history(wiki_root, "foo")

    _, rows = classify_status(tmp_path)

    assert len(rows) == 1
    assert rows[0].state == "stale-pin"
    assert "not reachable" in (rows[0].reason or "")


def test_dirty_takes_priority_over_behind(wiki_root: Path, tmp_path: Path) -> None:
    """If both dirty and pin != HEAD, dirty wins (single state per row)."""
    _initialized_wiki(wiki_root)
    old_pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, old_pin)
    edited = tmp_path / ".memtomem" / "skills" / "foo" / "SKILL.md"
    edited.write_bytes(b"local edit\n")
    _bump_mtime(edited)
    _modify_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v2\n"})

    _, rows = classify_status(tmp_path)

    assert rows[0].state == "dirty"
    assert rows[0].dirty_file_count == 1


def test_iter_order_alphabetical_by_type_then_name(wiki_root: Path, tmp_path: Path) -> None:
    """Rows preserve iter_entries() order: agents → commands → skills, alpha within."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "zeta", {"SKILL.md": b"\n"}, pin)
    _setup_installed_at_pin(tmp_path, "skills", "alpha", {"SKILL.md": b"\n"}, pin)
    _setup_installed_at_pin(tmp_path, "agents", "bar", {"agent.md": b"\n"}, pin)
    _setup_installed_at_pin(tmp_path, "commands", "baz", {"command.md": b"\n"}, pin)

    _, rows = classify_status(tmp_path)

    types = [r.asset_type for r in rows]
    names_skills = [r.name for r in rows if r.asset_type == "skills"]
    assert types == ["agents", "commands", "skills", "skills"]
    assert names_skills == ["alpha", "zeta"]


def test_no_write_invariant(monkeypatch, wiki_root: Path, tmp_path: Path) -> None:
    """classify_status must NEVER write — atomic_write_bytes is the only mutation primitive."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, pin)

    write_calls: list[tuple] = []

    original = atomic_write_bytes

    def fail_write(*args, **kwargs):
        write_calls.append((args, kwargs))
        return original(*args, **kwargs)

    import memtomem.context._atomic as _atomic_mod

    monkeypatch.setattr(_atomic_mod, "atomic_write_bytes", fail_write)
    monkeypatch.setattr("memtomem.context.lockfile.atomic_write_bytes", fail_write)

    classify_status(tmp_path)

    assert write_calls == []


# ── CLI tests ────────────────────────────────────────────────────────────


def test_cli_status_empty_lockfile_exits_0(
    wiki_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    _initialized_wiki(wiki_root)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["status"])
    assert result.exit_code == 0
    assert "No wiki assets installed" in result.output


def test_cli_status_renders_states(
    wiki_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, pin)

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["status"])

    assert result.exit_code == 0
    assert "skills" in result.output
    assert "foo" in result.output
    assert pin[:12] in result.output
    assert "Summary:" in result.output
    assert "1 ok" in result.output


def test_cli_status_corrupt_lockfile_exits_1(
    wiki_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    _initialized_wiki(wiki_root)
    lockfile_path = tmp_path / ".memtomem" / "lock.json"
    lockfile_path.parent.mkdir(parents=True)
    lockfile_path.write_text(json.dumps({"version": 999, "skills": {}}))

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["status"])

    assert result.exit_code == 1
    assert "lock.json" in result.output


def test_cli_status_wiki_absent_renders_with_annotation(
    wiki_root: Path,  # fixture sets MEMTOMEM_WIKI_PATH but we don't init
    tmp_path: Path,
    monkeypatch,
) -> None:
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, "0" * 40)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["status"])

    assert result.exit_code == 0
    assert "wiki not present" in result.output
    assert "foo" in result.output


# ── unused-fixture helpers (silence ruff F401) ───────────────────────────

_ = pytest  # keep pytest import alive if no test uses it directly


# Type-check helper to ensure StatusRow shape stays exported (catches refactors).
def test_status_row_dataclass_shape() -> None:
    row = StatusRow(
        asset_type="skills",
        name="foo",
        pin_commit="0" * 40,
        installed_at="2026-05-01T00:00:00Z",
        state="ok",
        dirty_file_count=0,
        reason=None,
    )
    assert row.asset_type == "skills"
    assert row.dirty_file_count == 0
