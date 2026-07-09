"""Tests for ``mm context adopt`` — verify-then-record provenance (#1684).

Adopt lockfile-tracks an existing ``project_shared`` canonical whose bytes
match the wiki HEAD asset, without writing a single dest byte. These cover
the engine (:func:`memtomem.context.install._adopt_asset` via its three
public wrappers) and the CLI surface: happy path across all three kinds
(dest untouched, entry shape identical to a fresh install's), the per-file
mismatch refusal in every category, the inverse state gate (already
tracked / nothing to adopt), install's reproducible-pin gates (HEAD
presence, same-asset wiki dirt), Gate A over the pinned bytes, skip-filter
parity with the copier, and the post-adopt invariants (update sees the
asset clean at HEAD; the untracked status row disappears).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem import privacy
from memtomem.cli.context_cmd import context as context_group
from memtomem.context.install import (
    AdoptMismatchError,
    AlreadyInstalledError,
    AssetNotFoundError,
    NotInstalledError,
    UncommittedAssetError,
    adopt_agent,
    adopt_command,
    adopt_skill,
    install_skill,
    update_skill,
)
from memtomem.context.lockfile import LOCKFILE_VERSION, Lockfile
from memtomem.context.privacy_scan import PrivacyBlockedError
from memtomem.context.status import classify_status
from memtomem.wiki.store import WikiStore

# ``wiki_root`` / ``git_identity`` fixtures come from conftest.py.

# AKIA fixture per feedback_force_unsafe_redaction_valve_only.md — a clean
# string never trips the scan, so the Gate A assertions below would
# false-pass without it.
SECRET = "api_key=AKIA1234567890ABCDEF"

_ADOPT_VERB = {"skills": adopt_skill, "agents": adopt_agent, "commands": adopt_command}
_MANIFEST = {"skills": "SKILL.md", "agents": "agent.md", "commands": "command.md"}


# ── helpers ──────────────────────────────────────────────────────────────


def _git_commit_all(wiki_root_path: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", message],
        check=True,
        capture_output=True,
    )


def _write_wiki_files(
    wiki_root_path: Path, asset_type: str, name: str, files: dict[str, bytes]
) -> None:
    """Drop asset files into the wiki working tree WITHOUT committing."""
    asset_dir = wiki_root_path / asset_type / name
    asset_dir.mkdir(parents=True, exist_ok=True)
    for relpath, data in files.items():
        target = asset_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _seed_wiki_asset(
    wiki_root_path: Path, asset_type: str, name: str, files: dict[str, bytes]
) -> None:
    """Drop an asset into an initialized wiki and commit."""
    _write_wiki_files(wiki_root_path, asset_type, name, files)
    _git_commit_all(wiki_root_path, f"add {asset_type}/{name}")


def _initialized_wiki(wiki_root_path: Path) -> WikiStore:
    store = WikiStore.at_default()
    store.init()
    return store


def _place_dest(project: Path, asset_type: str, name: str, files: dict[str, bytes]) -> Path:
    """Hand-place a dest tree at ``<project>/.memtomem/<asset_type>/<name>/``."""
    dest = project / ".memtomem" / asset_type / name
    dest.mkdir(parents=True, exist_ok=True)
    for relpath, data in files.items():
        target = dest / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return dest


def _tree_snapshot(dest: Path) -> dict[str, tuple[int, bytes]]:
    """rel → (mtime_ns, bytes) for every file under dest — adopt must not move it."""
    return {
        f.relative_to(dest).as_posix(): (f.stat().st_mtime_ns, f.read_bytes())
        for f in sorted(dest.rglob("*"))
        if f.is_file()
    }


_FILES = {
    "SKILL.md": b"# demo\n",
    "scripts/run.sh": b"#!/bin/bash\necho hi\n",
}


# ── happy path across kinds ──────────────────────────────────────────────


@pytest.mark.parametrize("asset_type", ["skills", "agents", "commands"])
def test_adopt_records_install_shaped_entry_without_touching_dest(
    wiki_root: Path, tmp_path: Path, asset_type: str
) -> None:
    store = _initialized_wiki(wiki_root)
    files = {_MANIFEST[asset_type]: b"# demo\n", "extra/notes.md": b"notes\n"}
    _seed_wiki_asset(wiki_root, asset_type, "demo", files)
    project = tmp_path / "proj"
    dest = _place_dest(project, asset_type, "demo", files)
    before = _tree_snapshot(dest)

    result = _ADOPT_VERB[asset_type](project, "demo")

    head = store.current_commit()
    assert result.asset_type == asset_type
    assert result.name == "demo"
    assert result.wiki_commit == head
    assert result.files_verified == 2
    assert result.dest == dest
    # No dest byte written or moved — bytes AND mtimes identical.
    assert _tree_snapshot(dest) == before

    entry = Lockfile.at(project).read_entry(asset_type, "demo")
    assert entry is not None
    assert entry["wiki_commit"] == head
    assert entry["files_commit"] == head
    assert entry["files"] == sorted(files)
    assert entry["digests"] == {
        rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()
    }
    assert entry["installed_at"] == result.installed_at

    lock_doc = json.loads((project / ".memtomem" / "lock.json").read_text())
    assert lock_doc["version"] == LOCKFILE_VERSION


def test_adopt_flips_status_untracked_to_ok(wiki_root: Path, tmp_path: Path) -> None:
    """The verb converts exactly the ``untracked`` status row into a tracked one."""
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "demo", _FILES)
    project = tmp_path / "proj"
    _place_dest(project, "skills", "demo", _FILES)

    def _states() -> dict[tuple[str, str], str]:
        _, rows = classify_status(project)
        return {(r.asset_type, r.name): r.state for r in rows}

    assert _states()[("skills", "demo")] == "untracked"
    adopt_skill(project, "demo")
    assert _states()[("skills", "demo")] == "ok"


def test_adopted_asset_is_clean_at_head_for_update(wiki_root: Path, tmp_path: Path) -> None:
    """Post-adopt invariant: the recorded digests are the proven-equal hashes,
    so ``mm context update`` classifies the asset as pin-at-HEAD no-op —
    not dirty, not behind."""
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "demo", _FILES)
    project = tmp_path / "proj"
    _place_dest(project, "skills", "demo", _FILES)

    adopt_skill(project, "demo")
    result = update_skill(project, "demo")

    assert result.was_no_op is True
    assert result.files_written == 0


# ── mismatch refusal — every category, zero lockfile residue ─────────────


def test_adopt_refuses_content_diff_with_per_file_report(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "demo", _FILES)
    project = tmp_path / "proj"
    dest = _place_dest(
        project, "skills", "demo", {**_FILES, "SKILL.md": b"# demo (edited locally)\n"}
    )
    before = _tree_snapshot(dest)

    with pytest.raises(AdoptMismatchError) as excinfo:
        adopt_skill(project, "demo")

    msg = str(excinfo.value)
    assert "differs: SKILL.md" in msg
    # Both exits are offered: take HEAD's bytes, or promote the local edits.
    assert "mm context install skill demo" in msg
    assert "mm wiki skill promote demo" in msg
    assert Lockfile.at(project).read_entry("skills", "demo") is None
    assert _tree_snapshot(dest) == before  # refusal is read-only too


def test_adopt_refuses_dest_only_and_head_only_files(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "demo", _FILES)
    project = tmp_path / "proj"
    _place_dest(
        project,
        "skills",
        "demo",
        {"SKILL.md": _FILES["SKILL.md"], "local-addition.md": b"mine\n"},
    )

    with pytest.raises(AdoptMismatchError) as excinfo:
        adopt_skill(project, "demo")

    msg = str(excinfo.value)
    assert "only on disk: local-addition.md" in msg
    assert "only at HEAD: scripts/run.sh" in msg
    assert "2 file(s)" in msg
    assert Lockfile.at(project).read_entry("skills", "demo") is None


# ── inverse state gate ───────────────────────────────────────────────────


def test_adopt_refuses_already_tracked_asset(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "demo", _FILES)
    project = tmp_path / "proj"
    project.mkdir()
    install_skill(project, "demo")

    with pytest.raises(AlreadyInstalledError) as excinfo:
        adopt_skill(project, "demo")

    assert "already lockfile-tracked" in str(excinfo.value)
    assert "mm context update skill demo" in str(excinfo.value)


def test_adopt_refuses_missing_dest(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "demo", _FILES)
    project = tmp_path / "proj"
    project.mkdir()

    with pytest.raises(NotInstalledError) as excinfo:
        adopt_skill(project, "demo")

    assert "nothing to adopt" in str(excinfo.value)
    assert "mm context install skill demo" in str(excinfo.value)
    assert Lockfile.at(project).read_entry("skills", "demo") is None


# ── reproducible-pin gates (shared contract with install, #1643) ─────────


def test_adopt_refuses_worktree_only_asset(wiki_root: Path, tmp_path: Path) -> None:
    """An asset never committed can't be pinned — same gate as install."""
    _initialized_wiki(wiki_root)
    _write_wiki_files(wiki_root, "skills", "demo", _FILES)  # no commit
    project = tmp_path / "proj"
    _place_dest(project, "skills", "demo", _FILES)

    with pytest.raises(UncommittedAssetError) as excinfo:
        adopt_skill(project, "demo")

    assert "never been committed" in str(excinfo.value)
    assert Lockfile.at(project).read_entry("skills", "demo") is None


def test_adopt_refuses_asset_absent_from_wiki(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki(wiki_root)
    project = tmp_path / "proj"
    _place_dest(project, "skills", "demo", _FILES)

    with pytest.raises(AssetNotFoundError):
        adopt_skill(project, "demo")


def test_adopt_refuses_dirty_wiki_asset(wiki_root: Path, tmp_path: Path) -> None:
    """Wiki worktree ≠ HEAD for THIS asset refuses even when dest matches
    HEAD exactly — the user-visible wiki bytes must equal the verified pin."""
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "demo", _FILES)
    (wiki_root / "skills" / "demo" / "SKILL.md").write_bytes(b"# demo (wiki edit)\n")
    project = tmp_path / "proj"
    _place_dest(project, "skills", "demo", _FILES)  # matches HEAD, not worktree

    with pytest.raises(UncommittedAssetError) as excinfo:
        adopt_skill(project, "demo")

    assert "differs from HEAD" in str(excinfo.value)
    assert Lockfile.at(project).read_entry("skills", "demo") is None


# ── skip-filter parity with the copier ───────────────────────────────────


def test_adopt_ignores_skip_listed_files_on_both_sides(wiki_root: Path, tmp_path: Path) -> None:
    """Compare set == the copier's write set: a wiki-side ``.bak`` (gitignored
    there anyway) and a dest-side ``.bak`` are both outside it, so neither
    blocks the adopt."""
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(
        wiki_root, "skills", "demo", {"SKILL.md": b"# demo\n", "old.md.bak": b"wiki bak\n"}
    )
    project = tmp_path / "proj"
    _place_dest(project, "skills", "demo", {"SKILL.md": b"# demo\n", "local.md.bak": b"dest bak\n"})

    result = adopt_skill(project, "demo")

    assert result.files_verified == 1
    entry = Lockfile.at(project).read_entry("skills", "demo")
    assert entry is not None
    assert entry["files"] == ["SKILL.md"]


def test_adopt_all_skipped_asset_matches_install_parity(wiki_root: Path, tmp_path: Path) -> None:
    """An asset whose committed files are ALL skip-listed installs with
    ``files=[]``/``digests={}`` — adopt must accept the same asset the same
    way, not refuse it (Codex review fold)."""
    _initialized_wiki(wiki_root)
    ds = wiki_root / "skills" / "dsonly" / ".DS_Store"
    ds.parent.mkdir(parents=True)
    ds.write_bytes(b"finder junk")
    subprocess.run(
        ["git", "-C", str(wiki_root), "add", "-f", "skills/dsonly/.DS_Store"],
        check=True,
        capture_output=True,
    )
    _git_commit_all(wiki_root, "add dsonly")
    project = tmp_path / "proj"
    # Dest holds only skip-listed content too — compares empty on both sides.
    _place_dest(project, "skills", "dsonly", {"local.md.bak": b"dest bak\n"})

    result = adopt_skill(project, "dsonly")

    assert result.files_verified == 0
    entry = Lockfile.at(project).read_entry("skills", "dsonly")
    assert entry is not None
    assert entry["files"] == []
    assert entry["digests"] == {}


requires_posix_perms = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permissions and a non-root user",
)


@requires_posix_perms
def test_adopt_unreadable_dest_subdir_is_classified_refusal(
    wiki_root: Path, tmp_path: Path
) -> None:
    """``iter_installed_files`` is fail-closed and raises mid-walk on an
    unreadable directory; adopt must surface that as its own classified
    refusal (can't prove equal), never a raw ``PermissionError`` traceback."""
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "demo", _FILES)
    project = tmp_path / "proj"
    dest = _place_dest(project, "skills", "demo", _FILES)
    locked = dest / "scripts"
    locked.chmod(0o000)
    try:
        with pytest.raises(AdoptMismatchError) as excinfo:
            adopt_skill(project, "demo")
    finally:
        locked.chmod(0o755)

    assert "unreadable path during the dest walk" in str(excinfo.value)
    assert Lockfile.at(project).read_entry("skills", "demo") is None


# ── Gate A over the pinned bytes ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_privacy_counters():
    """Zeroed counters so surface-attribution asserts see only their own test."""
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


def test_adopt_blocks_secret_asset_no_bypass(wiki_root: Path, tmp_path: Path) -> None:
    """Hand-copy-then-adopt must not reach a tracked state install refuses:
    Gate A scans the pinned bytes and blocks BEFORE the lockfile write,
    attributed to the adopt ingress surface."""
    _initialized_wiki(wiki_root)
    files = {"SKILL.md": b"# leak\n" + SECRET.encode() + b"\n"}
    _seed_wiki_asset(wiki_root, "skills", "leak", files)
    project = tmp_path / "proj"
    _place_dest(project, "skills", "leak", files)

    with pytest.raises(PrivacyBlockedError) as excinfo:
        adopt_skill(project, "leak")

    assert Lockfile.at(project).read_entry("skills", "leak") is None
    # Matched bytes never reach the error message.
    assert "AKIA1234567890ABCDEF" not in excinfo.value.message
    by_tool = privacy.snapshot()["by_tool"]
    assert by_tool.get("cli_context_adopt", {}).get("blocked", 0) == 1
    assert "cli_context_install" not in by_tool


def test_adopt_mismatch_report_wins_over_gate_a(wiki_root: Path, tmp_path: Path) -> None:
    """Gate A runs after the byte compare: a mismatched dest gets the
    contract's per-file diff report even when the pinned wiki bytes carry a
    secret — the privacy refusal is reserved for the case adopt would
    actually bless (Codex review fold)."""
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "leak", {"SKILL.md": b"# leak\n" + SECRET.encode()})
    project = tmp_path / "proj"
    _place_dest(project, "skills", "leak", {"SKILL.md": b"# different\n"})

    with pytest.raises(AdoptMismatchError) as excinfo:
        adopt_skill(project, "leak")

    assert "differs: SKILL.md" in str(excinfo.value)
    assert "AKIA1234567890ABCDEF" not in str(excinfo.value)
    assert Lockfile.at(project).read_entry("skills", "leak") is None
    # The scan never ran — nothing was about to be blessed.
    assert privacy.snapshot()["by_tool"].get("cli_context_adopt", {}).get("blocked", 0) == 0


# ── CLI surface ──────────────────────────────────────────────────────────


@pytest.fixture
def project_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Tmp project root (with a sentinel ``.git``) wired as cwd so
    ``_find_project_root`` resolves there."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.chdir(project)
    return project


def test_cli_adopt_success(wiki_root: Path, project_cwd: Path) -> None:
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "demo", _FILES)
    _place_dest(project_cwd, "skills", "demo", _FILES)

    runner = CliRunner()
    result = runner.invoke(context_group, ["adopt", "skill", "demo"])

    assert result.exit_code == 0, result.output
    assert "Adopted skills/demo" in result.output
    assert "2 file(s) verified byte-identical" in result.output


def test_cli_adopt_mismatch_exit_and_report(wiki_root: Path, project_cwd: Path) -> None:
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "demo", _FILES)
    _place_dest(project_cwd, "skills", "demo", {**_FILES, "SKILL.md": b"edited\n"})

    runner = CliRunner()
    result = runner.invoke(context_group, ["adopt", "skill", "demo"])

    assert result.exit_code == 1, result.output
    assert "differs: SKILL.md" in result.output
    assert "Traceback" not in result.output


def test_cli_install_refusal_hints_adopt(wiki_root: Path, project_cwd: Path) -> None:
    """The dest-exists-no-lock refusal now names the explicit exit (#1684)."""
    _initialized_wiki(wiki_root)
    _seed_wiki_asset(wiki_root, "skills", "demo", _FILES)
    _place_dest(project_cwd, "skills", "demo", _FILES)

    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "skill", "demo"])

    assert result.exit_code == 1, result.output
    assert "mm context adopt skill demo" in result.output
