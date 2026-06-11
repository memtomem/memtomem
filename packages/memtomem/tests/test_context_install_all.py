"""Tests for ``mm context install --all`` (PR-D C3 commit 2, ADR-0008).

Option A semantics: walk ``<project>/.memtomem/lock.json`` and re-install
each entry **at the commit each entry pins** (NOT wiki HEAD). Exercises:

- byte-identity at pin (fresh-machine restore is reproducible)
- 5-state classification (install / skip / refuse / orphan / error)
- ``--force`` semantics (skip → re-extract; refuse → .bak + extract)
- pin invariance (lockfile.wiki_commit unchanged post-install)
- orphan handling (pin not reachable → batch continues)
- empty / corrupt lockfile paths

These tests reuse the C2 fixture pattern (``wiki_root``, ``git_identity``)
and construct dest + lockfile via the live install path so the
``installed_at`` invariant from C2a (#630) holds.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context as context_group
from memtomem.context.install import (
    install_skill,
    install_agent,
    install_command,
)
from memtomem.context.lockfile import Lockfile
from memtomem.wiki.store import WikiStore


# ── helpers ──────────────────────────────────────────────────────────────


def _initialized_wiki(wiki_root_path: Path) -> WikiStore:
    store = WikiStore.at_default()
    store.init()
    return store


def _seed_wiki_asset(
    wiki_root_path: Path,
    asset_type: str,
    name: str,
    files: dict[str, bytes],
) -> str:
    asset_dir = wiki_root_path / asset_type / name
    asset_dir.mkdir(parents=True, exist_ok=True)
    for relpath, data in files.items():
        target = asset_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", f"add {asset_type}/{name}"],
        check=True,
        capture_output=True,
    )
    return WikiStore.at_default().current_commit()


def _advance_wiki(wiki_root_path: Path, asset_type: str, name: str, files: dict[str, bytes]) -> str:
    asset_dir = wiki_root_path / asset_type / name
    for relpath, data in files.items():
        target = asset_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", f"advance {asset_type}/{name}"],
        check=True,
        capture_output=True,
    )
    return WikiStore.at_default().current_commit()


def _bump_mtime(path: Path, *, seconds_in_future: float = 1.0) -> None:
    import os
    from datetime import datetime, timezone

    future = datetime.now(timezone.utc).timestamp() + seconds_in_future
    os.utime(path, (future, future))


def _make_orphan(wiki_root_path: Path, name: str) -> None:
    """Hard-reset wiki history so the previously-pinned commit is unreachable."""
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
        ["git", "-C", str(wiki_root_path), "commit", "-m", "rewrite"],
        check=True,
        capture_output=True,
    )
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


# ── empty / no-op paths ─────────────────────────────────────────────────


def test_empty_lockfile_exits_zero(wiki_root: Path, tmp_path: Path, monkeypatch) -> None:
    _initialized_wiki(wiki_root)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "--all"])
    assert result.exit_code == 0
    assert "No entries in lock.json" in result.output


def test_all_entries_already_in_sync_no_op(wiki_root: Path, tmp_path: Path, monkeypatch) -> None:
    """Every dest exists and is clean → all rows skip, exit 0, no writes."""
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "foo", {"SKILL.md": b"v1\n"})
    install_skill(tmp_path, "foo")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "--all"], input="y\n")

    assert result.exit_code == 0
    assert "Nothing to install" in result.output


# ── happy paths ─────────────────────────────────────────────────────────


def test_fresh_restore_byte_identical_to_pin(wiki_root: Path, tmp_path: Path, monkeypatch) -> None:
    """The reproducibility invariant: dest bytes match the pinned commit's bytes."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_asset(wiki_root, "skills", "foo", {"SKILL.md": b"pinned-v1\n"})
    install_skill(tmp_path, "foo")
    # Wiki advances past the pin.
    _advance_wiki(wiki_root, "skills", "foo", {"SKILL.md": b"head-v2\n"})

    # Simulate fresh checkout: dest dir gone, lockfile retained.
    shutil.rmtree(tmp_path / ".memtomem" / "skills" / "foo")
    assert not (tmp_path / ".memtomem" / "skills" / "foo").exists()

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "--all", "--yes"])

    assert result.exit_code == 0, result.output
    # Dest restored at the PIN, not HEAD.
    assert (tmp_path / ".memtomem" / "skills" / "foo" / "SKILL.md").read_bytes() == b"pinned-v1\n"

    # Pin invariance: lockfile.wiki_commit unchanged.
    lock_doc = json.loads((tmp_path / ".memtomem" / "lock.json").read_text())
    assert lock_doc["skills"]["foo"]["wiki_commit"] == pin


def test_mixed_install_and_skip(wiki_root: Path, tmp_path: Path, monkeypatch) -> None:
    """One entry missing dest (install), one already present (skip) → 1 install + 1 skip."""
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "foo", {"SKILL.md": b"foo\n"})
    install_skill(tmp_path, "foo")
    _seed_wiki_asset(wiki_root, "skills", "bar", {"SKILL.md": b"bar\n"})
    install_skill(tmp_path, "bar")

    # Remove only foo's dest.
    shutil.rmtree(tmp_path / ".memtomem" / "skills" / "foo")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "--all", "--yes"])

    assert result.exit_code == 0, result.output
    assert "1 installed" in result.output
    assert "1 skipped" in result.output
    assert (tmp_path / ".memtomem" / "skills" / "foo" / "SKILL.md").exists()
    assert (tmp_path / ".memtomem" / "skills" / "bar" / "SKILL.md").exists()


# ── refuse / force paths ────────────────────────────────────────────────


def test_dirty_refuse_without_force_exits_1(wiki_root: Path, tmp_path: Path, monkeypatch) -> None:
    """Local edits + no --force → batch refuses, no writes."""
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "foo", {"SKILL.md": b"v1\n"})
    install_skill(tmp_path, "foo")
    edited = tmp_path / ".memtomem" / "skills" / "foo" / "SKILL.md"
    edited.write_bytes(b"manual edit\n")
    _bump_mtime(edited)

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "--all"])

    assert result.exit_code == 1
    assert "have local edits" in result.output
    # No writes — dirty content survives.
    assert edited.read_bytes() == b"manual edit\n"


def test_dirty_force_writes_bak_and_restores_at_pin(
    wiki_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """``--force`` preserves dirty as .bak then restores from pin."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_asset(wiki_root, "skills", "foo", {"SKILL.md": b"pinned\n"})
    install_skill(tmp_path, "foo")
    edited = tmp_path / ".memtomem" / "skills" / "foo" / "SKILL.md"
    edited.write_bytes(b"local edit\n")
    _bump_mtime(edited)

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "--all", "--yes", "--force"])

    assert result.exit_code == 0, result.output
    # Pin bytes restored.
    assert edited.read_bytes() == b"pinned\n"
    # .bak preserved with the user edit.
    bak = edited.with_suffix(edited.suffix + ".bak")
    assert bak.exists()
    assert bak.read_bytes() == b"local edit\n"
    # Pin invariance.
    lock_doc = json.loads((tmp_path / ".memtomem" / "lock.json").read_text())
    assert lock_doc["skills"]["foo"]["wiki_commit"] == pin


def test_yes_force_emits_destructive_warning(wiki_root: Path, tmp_path: Path, monkeypatch) -> None:
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "foo", {"SKILL.md": b"v1\n"})
    install_skill(tmp_path, "foo")
    edited = tmp_path / ".memtomem" / "skills" / "foo" / "SKILL.md"
    edited.write_bytes(b"edit\n")
    _bump_mtime(edited)

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "--all", "--yes", "--force"])

    assert result.exit_code == 0
    assert "WARNING" in result.output
    assert "destructive" in result.output.lower()


# ── orphan / error paths ────────────────────────────────────────────────


def test_orphan_pin_single_entry(wiki_root: Path, tmp_path: Path, monkeypatch) -> None:
    """Pin not reachable → state=orphan, exit 0 (informational, not error)."""
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "foo", {"SKILL.md": b"v1\n"})
    install_skill(tmp_path, "foo")
    _make_orphan(wiki_root, "foo")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "--all", "--yes"])

    assert "orphan" in result.output.lower()
    # Orphan-only batch: nothing actionable, exit 0 (orphan is a warning, not error).
    assert result.exit_code == 0


def test_orphan_with_reachable_sibling_via_branch_drop(
    wiki_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """Orphan one entry's pin while keeping a sibling's pin reachable.

    Mechanism: install ``orphaned`` on a feature branch, switch back to main
    (deleting the branch + reflog), then install ``good`` on main. The
    feature-branch commit is no longer reachable from any ref, but main
    HEAD still carries ``good``'s pin.
    """
    _initialized_wiki(wiki_root)

    # Branch off main, seed "orphaned", install — lockfile pin1 = feature-branch tip.
    subprocess.run(
        ["git", "-C", str(wiki_root), "checkout", "-b", "feature"],
        check=True,
        capture_output=True,
    )
    _seed_wiki_asset(wiki_root, "skills", "orphaned", {"SKILL.md": b"on-feature\n"})
    install_skill(tmp_path, "orphaned")

    # Back to main, delete feature branch + reflog so pin1 is unreachable.
    subprocess.run(
        ["git", "-C", str(wiki_root), "checkout", "main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wiki_root), "branch", "-D", "feature"],
        check=True,
        capture_output=True,
    )
    # Remove the orphaned working-tree dir (it doesn't exist on main).
    shutil.rmtree(wiki_root / "skills" / "orphaned", ignore_errors=True)

    # Seed "good" on main, install — pin2 reachable.
    _seed_wiki_asset(wiki_root, "skills", "good", {"SKILL.md": b"on-main\n"})
    install_skill(tmp_path, "good")

    # Expire reflog so pin1 is fully unreachable.
    subprocess.run(
        ["git", "-C", str(wiki_root), "reflog", "expire", "--expire=now", "--all"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wiki_root), "gc", "--prune=now", "--quiet"],
        check=True,
        capture_output=True,
    )

    # Drop "good"'s dest so install --all has something to do alongside the orphan.
    shutil.rmtree(tmp_path / ".memtomem" / "skills" / "good")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "--all", "--yes"])

    # "good" installed, "orphaned" reported as orphan, exit 0.
    assert (tmp_path / ".memtomem" / "skills" / "good" / "SKILL.md").exists()
    assert (tmp_path / ".memtomem" / "skills" / "good" / "SKILL.md").read_bytes() == b"on-main\n"
    assert "orphan" in result.output.lower()
    assert "1 installed" in result.output
    assert result.exit_code == 0


# ── argument validation ────────────────────────────────────────────────


def test_all_with_positional_args_rejects(wiki_root: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "--all", "skill", "foo"])
    assert result.exit_code != 0
    assert "no <type> <name>" in result.output


def test_yes_without_all_rejects(wiki_root: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "skill", "foo", "--yes"])
    assert result.exit_code != 0
    assert "only valid with --all" in result.output


def test_force_without_all_rejects(wiki_root: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "skill", "foo", "--force"])
    assert result.exit_code != 0
    assert "only valid with --all" in result.output


def test_install_without_args_rejects(wiki_root: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install"])
    assert result.exit_code != 0
    assert "<type> <name>" in result.output or "--all" in result.output


def test_wiki_absent_clickexception(wiki_root: Path, tmp_path: Path, monkeypatch) -> None:
    """No wiki → clean ClickException, no traceback."""
    # Lockfile with one entry, but wiki not initialized.
    Lockfile.at(tmp_path).upsert_entry(
        "skills", "foo", wiki_commit="0" * 40, installed_at="2026-05-01T00:00:00.000000Z"
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "--all"])
    assert result.exit_code != 0
    assert "wiki" in result.output.lower()


# ── multiple asset types ────────────────────────────────────────────────


def test_mixed_asset_types(wiki_root: Path, tmp_path: Path, monkeypatch) -> None:
    """Install --all walks skills + agents + commands together."""
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "foo", {"SKILL.md": b"foo\n"})
    install_skill(tmp_path, "foo")
    _seed_wiki_asset(wiki_root, "agents", "bar", {"agent.md": b"bar\n"})
    install_agent(tmp_path, "bar")
    _seed_wiki_asset(wiki_root, "commands", "baz", {"command.md": b"baz\n"})
    install_command(tmp_path, "baz")

    # Remove all dests.
    for asset_type, name in [("skills", "foo"), ("agents", "bar"), ("commands", "baz")]:
        shutil.rmtree(tmp_path / ".memtomem" / asset_type / name)

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "--all", "--yes"])

    assert result.exit_code == 0, result.output
    assert "3 installed" in result.output
    assert (tmp_path / ".memtomem" / "skills" / "foo" / "SKILL.md").exists()
    assert (tmp_path / ".memtomem" / "agents" / "bar" / "agent.md").exists()
    assert (tmp_path / ".memtomem" / "commands" / "baz" / "command.md").exists()


# ── classify→execute race ───────────────────────────────────────────────


def test_classify_execute_race_pin_pruned_mid_loop(
    wiki_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """Pin reachable at classify time but pruned before extract → orphan row, batch continues.

    The race window is real: ``_classify_for_install_all`` calls
    ``commit_is_reachable`` once up front; if a concurrent ``git gc
    --prune=now`` removes the commit before ``_apply_pinned_install`` runs,
    the inner ``copy_asset_at_commit`` re-checks reachability and raises
    ``CommitNotFoundError``. The CLI loop catches it (``context_cmd.py``
    line 1218-1221) and reclassifies the row as orphan without crashing
    the batch.

    Simulated here by monkey-patching ``WikiStore.copy_asset_at_commit``
    to raise ``CommitNotFoundError`` on the first call only — the second
    call (a sibling that should succeed) goes through unmodified.
    """
    from memtomem.wiki.store import CommitNotFoundError, WikiStore as _WikiStore

    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "raced", {"SKILL.md": b"raced\n"})
    install_skill(tmp_path, "raced")
    _seed_wiki_asset(wiki_root, "skills", "winner", {"SKILL.md": b"winner\n"})
    install_skill(tmp_path, "winner")

    # Both dests gone → both classify as state=install (no dirty walk).
    shutil.rmtree(tmp_path / ".memtomem" / "skills" / "raced")
    shutil.rmtree(tmp_path / ".memtomem" / "skills" / "winner")

    real_copy = _WikiStore.copy_asset_at_commit
    raise_for: dict[str, bool] = {"raced": True}

    def flaky_copy(self, commit, asset_type, name, dest):
        if raise_for.get(name, False):
            raise CommitNotFoundError(f"simulated race: {commit[:12]} pruned")
        return real_copy(self, commit, asset_type, name, dest)

    monkeypatch.setattr(_WikiStore, "copy_asset_at_commit", flaky_copy)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "--all", "--yes"])

    # Race row reported as orphan; sibling installs successfully.
    assert "raced" in result.output
    assert "simulated race" in result.output
    assert (tmp_path / ".memtomem" / "skills" / "winner" / "SKILL.md").exists()
    assert not (tmp_path / ".memtomem" / "skills" / "raced" / "SKILL.md").exists()
    assert "1 installed" in result.output
    assert "1 orphaned" in result.output
    # Orphan-only failures don't fail the batch (warning, not error).
    assert result.exit_code == 0


# silence "imported but unused" if a future test needs the fixture
_ = pytest


# ── B1: pinned re-extraction reconciles dest-only files (#1247) ──────────


def test_force_reextraction_removes_stale_dest_only_file(
    wiki_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """``install --all --force`` re-extracts at the pin; a dest-only file
    whose mtime predates ``installed_at`` (a pre-B1 additive-update leftover)
    must be reconciled away, not carried forever."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_asset(wiki_root, "skills", "foo", {"SKILL.md": b"pinned\n"})
    install_skill(tmp_path, "foo")

    dest = tmp_path / ".memtomem" / "skills" / "foo"
    stale = dest / "leftover.md"
    stale.write_bytes(b"stale wiki bytes\n")
    # Backdate so the file reads as old wiki bytes (mtime <= installed_at):
    # the legacy-entry deletion rule, not the user-added keep rule.
    past = datetime.now(timezone.utc).timestamp() - 3600
    os.utime(stale, (past, past))
    # Pre-B1 entries carry no manifest — simulate one by stripping the keys.
    lock_path = tmp_path / ".memtomem" / "lock.json"
    doc = json.loads(lock_path.read_text(encoding="utf-8"))
    doc["skills"]["foo"].pop("files", None)
    doc["skills"]["foo"].pop("files_commit", None)
    lock_path.write_text(json.dumps(doc), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "--all", "--yes", "--force"])

    assert result.exit_code == 0, result.output
    assert not stale.exists()
    assert (dest / "SKILL.md").read_bytes() == b"pinned\n"
    lock_doc = json.loads(lock_path.read_text(encoding="utf-8"))
    assert lock_doc["skills"]["foo"]["wiki_commit"] == pin
    # Manifest recorded against the pin on re-extraction.
    assert lock_doc["skills"]["foo"]["files"] == ["SKILL.md"]
    assert lock_doc["skills"]["foo"]["files_commit"] == pin


def test_classify_install_all_missing_only_dirty_reason(wiki_root: Path, tmp_path: Path) -> None:
    """Codex implementation-gate M2: a manifest-detected deletion with zero
    modified files must not classify-print as '0 file(s) modified locally'."""
    from memtomem.context.install import _classify_for_install_all

    store = _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "foo", {"SKILL.md": b"x\n", "gone.md": b"y\n"})
    install_skill(tmp_path, "foo")
    (tmp_path / ".memtomem" / "skills" / "foo" / "gone.md").unlink()

    rows = _classify_for_install_all(tmp_path, wiki=store)

    assert len(rows) == 1
    assert rows[0].state == "refuse"
    assert rows[0].reason is not None
    assert "deleted locally" in rows[0].reason
    assert "0 file(s) modified" not in rows[0].reason
