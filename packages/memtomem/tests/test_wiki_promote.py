"""Tests for ``mm wiki {skill,agent,command} promote`` and its engine.

Promote is the inbound verb of the wiki ↔ context-gateway lifecycle (#1683): it
copies a project's ``project_shared`` canonical into the host-global wiki,
privacy-scans every source file, lints the copy, and records it as one isolated
commit. These cover the engine (:func:`memtomem.wiki.promote.promote_asset`) and
the CLI surface: happy path across all three kinds, the hard privacy gate, the
non-destructive collision guard (working tree AND HEAD), lint-failure rollback,
exec-bit preservation, and skip-listed/symlink source files.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.wiki_cmd import wiki as wiki_group
from memtomem.wiki.promote import (
    PromoteLintError,
    PromotePrivacyError,
    PromoteSourceError,
    WikiAssetExistsError,
    promote_asset,
)
from memtomem.wiki.store import WikiStore

# ``wiki_root`` / ``git_identity`` fixtures come from conftest.py.

# AKIA fixture per feedback_force_unsafe_redaction_valve_only.md — trips the
# privacy scan; the raw value must never appear in any surfaced message.
SECRET = "api_key=AKIA1234567890ABCDEF"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


def _init_wiki() -> WikiStore:
    store = WikiStore.at_default()
    store.init()
    return store


def _seed_project_asset(
    project_root: Path,
    asset_type: str,
    name: str,
    manifest: str,
    body: bytes = b"# canonical\n",
) -> Path:
    """Create ``<project>/.memtomem/<asset_type>/<name>/<manifest>`` and return the dir."""
    d = project_root / ".memtomem" / asset_type / name
    d.mkdir(parents=True)
    (d / manifest).write_bytes(body)
    return d


def _combined(result) -> str:  # noqa: ANN001
    out = result.output or ""
    try:
        out += result.stderr
    except ValueError:
        pass
    return out


# ── engine: happy paths across kinds ────────────────────────────────────────


# Agents require YAML frontmatter to parse; skills are byte-copied and commands
# parse without it. Keep each kind's happy-path body minimally valid.
_VALID_BODY: dict[str, bytes] = {
    "skills": b"# canonical\n",
    "agents": b"---\nname: demo\ndescription: a test agent\n---\n\n# body\n",
    "commands": b"# canonical\n",
}


@pytest.mark.parametrize(
    ("asset_type", "manifest"),
    [("skills", "SKILL.md"), ("agents", "agent.md"), ("commands", "command.md")],
)
def test_promote_happy(wiki_root: Path, tmp_path: Path, asset_type: str, manifest: str) -> None:
    store = _init_wiki()
    project = tmp_path / "proj"
    _seed_project_asset(project, asset_type, "demo", manifest, body=_VALID_BODY[asset_type])
    head0 = store.current_commit()

    result = promote_asset(store, project, asset_type, "demo")  # type: ignore[arg-type]

    assert result.files_committed == 1
    assert result.wiki_dirty is False
    assert store.current_commit() != head0
    # Committed at HEAD and present in the working tree.
    assert store.asset_files_at_commit(store.current_commit(), asset_type, "demo") == [manifest]
    assert (wiki_root / asset_type / "demo" / manifest).is_file()
    assert store.is_dirty() is False


def test_promote_multi_file_skill_preserves_exec_bit(wiki_root: Path, tmp_path: Path) -> None:
    store = _init_wiki()
    project = tmp_path / "proj"
    src = _seed_project_asset(project, "skills", "demo", "SKILL.md")
    scripts = src / "scripts"
    scripts.mkdir()
    run = scripts / "run.sh"
    run.write_bytes(b"#!/bin/sh\necho hi\n")
    run.chmod(0o755)

    result = promote_asset(store, project, "skills", "demo")

    assert result.files_committed == 2
    head = store.current_commit()
    files = store.asset_files_at_commit(head, "skills", "demo")
    assert sorted(files) == ["SKILL.md", "scripts/run.sh"]
    # Exec bit rode along into the wiki working tree (commit_paths is disk-first).
    if os.name != "nt":
        assert os.access(wiki_root / "skills" / "demo" / "scripts" / "run.sh", os.X_OK)


# ── engine: privacy hard gate ───────────────────────────────────────────────


def test_promote_privacy_blocked_no_bytes_leaked(wiki_root: Path, tmp_path: Path) -> None:
    store = _init_wiki()
    project = tmp_path / "proj"
    _seed_project_asset(project, "skills", "demo", "SKILL.md", body=f"# skill\n{SECRET}\n".encode())

    with pytest.raises(PromotePrivacyError) as excinfo:
        promote_asset(store, project, "skills", "demo")

    assert "AKIA1234567890ABCDEF" not in str(excinfo.value)
    assert excinfo.value.rel == "SKILL.md"
    # Nothing landed in the wiki: no dir, no new commit, clean tree.
    assert not (wiki_root / "skills" / "demo").exists()
    assert store.is_dirty() is False


# ── engine: collision guard (non-destructive) ───────────────────────────────


def test_promote_refuses_existing_wiki_asset_at_head(wiki_root: Path, tmp_path: Path) -> None:
    store = _init_wiki()
    project = tmp_path / "proj"
    _seed_project_asset(project, "skills", "demo", "SKILL.md")
    # First promote lands it at HEAD.
    promote_asset(store, project, "skills", "demo")
    head_after_first = store.current_commit()

    # A second project with the same name must refuse (committed-at-HEAD half).
    project2 = tmp_path / "proj2"
    _seed_project_asset(project2, "skills", "demo", "SKILL.md", body=b"# different\n")
    with pytest.raises(WikiAssetExistsError):
        promote_asset(store, project2, "skills", "demo")
    assert store.current_commit() == head_after_first


def test_promote_refuses_existing_worktree_dir(wiki_root: Path, tmp_path: Path) -> None:
    store = _init_wiki()
    project = tmp_path / "proj"
    _seed_project_asset(project, "skills", "demo", "SKILL.md")
    # Hand-place an uncommitted dir in the wiki working tree.
    (wiki_root / "skills" / "demo").mkdir(parents=True)
    (wiki_root / "skills" / "demo" / "SKILL.md").write_bytes(b"# squatter\n")

    with pytest.raises(WikiAssetExistsError):
        promote_asset(store, project, "skills", "demo")
    # The squatter is untouched.
    assert (wiki_root / "skills" / "demo" / "SKILL.md").read_bytes() == b"# squatter\n"


# ── engine: source errors ───────────────────────────────────────────────────


def test_promote_missing_source(wiki_root: Path, tmp_path: Path) -> None:
    store = _init_wiki()
    with pytest.raises(PromoteSourceError):
        promote_asset(store, tmp_path / "proj", "skills", "nope")


def test_promote_dir_without_manifest(wiki_root: Path, tmp_path: Path) -> None:
    store = _init_wiki()
    project = tmp_path / "proj"
    # A skill dir with a file but no SKILL.md manifest.
    d = project / ".memtomem" / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "notes.md").write_bytes(b"# not a manifest\n")
    with pytest.raises(PromoteSourceError):
        promote_asset(store, project, "skills", "demo")


# ── engine: lint failure rolls back ─────────────────────────────────────────


def test_promote_lint_failure_rolls_back(wiki_root: Path, tmp_path: Path) -> None:
    store = _init_wiki()
    project = tmp_path / "proj"
    src = _seed_project_asset(project, "skills", "demo", "SKILL.md")
    # A stray override file (unregistered extension) makes lint_asset error.
    overrides = src / "overrides"
    overrides.mkdir()
    (overrides / "bogus.txt").write_bytes(b"not a real override\n")
    head0 = store.current_commit()

    with pytest.raises(PromoteLintError):
        promote_asset(store, project, "skills", "demo")

    # Rolled back: the copied dir is gone, no new commit, clean tree.
    assert not (wiki_root / "skills" / "demo").exists()
    assert store.current_commit() == head0
    assert store.is_dirty() is False


# ── engine: commit is in-memory + CAS-guarded ───────────────────────────────


def test_promote_commits_scanned_bytes_not_disk(
    wiki_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The committed blob is the scanned in-memory bytes, never a disk re-read.

    lint runs after the copy and before the commit; here it tampers with the
    on-disk file. commit_paths must still commit the original scanned bytes
    (``scan set == commit set``), so the tampered bytes never reach history.
    """
    store = _init_wiki()
    project = tmp_path / "proj"
    clean = b"# clean canonical\n"
    _seed_project_asset(project, "skills", "demo", "SKILL.md", body=clean)

    import memtomem.wiki.promote as promote_mod

    real_lint = promote_mod.lint_asset

    def tamper_then_lint(store_, asset_type, name):  # noqa: ANN001, ANN202
        # Overwrite the just-copied working-tree file with a secret. A
        # disk-re-reading commit would pick this up; commit_paths must not.
        (wiki_root / "skills" / "demo" / "SKILL.md").write_bytes(f"{SECRET}\n".encode())
        return real_lint(store_, asset_type, name)

    monkeypatch.setattr(promote_mod, "lint_asset", tamper_then_lint)

    result = promote_asset(store, project, "skills", "demo")

    committed = _git(wiki_root, "show", f"{result.wiki_head}:skills/demo/SKILL.md")
    assert committed == clean.decode()
    assert "AKIA1234567890ABCDEF" not in committed


def test_promote_refuses_and_rolls_back_when_head_moves(
    wiki_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A competing commit landing mid-promote fails the CAS and rolls back cleanly.

    lint (under the promote lock) lands an unrelated commit via raw git, which
    honours no lock and moves HEAD. commit_paths' ref CAS then fails; promote
    must remove only its own copied dir and leave the competitor intact.
    """
    from memtomem.wiki.store import WikiHeadMovedError

    store = _init_wiki()
    project = tmp_path / "proj"
    _seed_project_asset(project, "skills", "demo", "SKILL.md")

    import memtomem.wiki.promote as promote_mod

    real_lint = promote_mod.lint_asset

    def commit_competitor_then_lint(store_, asset_type, name):  # noqa: ANN001, ANN202
        other = wiki_root / "skills" / "other"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_bytes(b"# competitor\n")
        # Stage ONLY the competitor so our uncommitted demo/ is not swept in.
        _git(wiki_root, "add", "skills/other/SKILL.md")
        _git(wiki_root, "commit", "-m", "competitor landed mid-promote")
        return real_lint(store_, asset_type, name)

    monkeypatch.setattr(promote_mod, "lint_asset", commit_competitor_then_lint)

    with pytest.raises(WikiHeadMovedError):
        promote_asset(store, project, "skills", "demo")

    # Our copy was rolled back; the competitor survives at HEAD.
    assert not (wiki_root / "skills" / "demo").exists()
    assert (wiki_root / "skills" / "other" / "SKILL.md").is_file()
    head = store.current_commit()
    assert store.asset_files_at_commit(head, "skills", "other") == ["SKILL.md"]


# ── engine: skip-listed and symlink source files ────────────────────────────


def test_promote_skips_bak_and_dotgit(wiki_root: Path, tmp_path: Path) -> None:
    store = _init_wiki()
    project = tmp_path / "proj"
    src = _seed_project_asset(project, "skills", "demo", "SKILL.md")
    (src / "SKILL.md.bak").write_bytes(b"# stale backup\n")
    (src / ".DS_Store").write_bytes(b"junk")

    result = promote_asset(store, project, "skills", "demo")

    assert result.files_committed == 1
    files = store.asset_files_at_commit(store.current_commit(), "skills", "demo")
    assert files == ["SKILL.md"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_promote_skips_symlink_source(wiki_root: Path, tmp_path: Path) -> None:
    store = _init_wiki()
    project = tmp_path / "proj"
    src = _seed_project_asset(project, "skills", "demo", "SKILL.md")
    outside = tmp_path / "outside-secret.txt"
    outside.write_bytes(f"{SECRET}\n".encode())
    (src / "link.md").symlink_to(outside)

    result = promote_asset(store, project, "skills", "demo")

    # The symlink was skipped (not dereferenced into wiki history).
    assert result.files_committed == 1
    assert not (wiki_root / "skills" / "demo" / "link.md").exists()


# ── CLI surface ─────────────────────────────────────────────────────────────


def test_cli_promote_happy(wiki_root: Path, tmp_path: Path) -> None:
    _init_wiki()
    project = tmp_path / "proj"
    _seed_project_asset(project, "commands", "demo", "command.md")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "promote", "demo", "--project", str(project)])

    assert result.exit_code == 0, _combined(result)
    assert "Promoted commands/demo" in result.output
    assert "mm context install command demo" in result.output


def test_cli_promote_privacy_blocked_exit1(wiki_root: Path, tmp_path: Path) -> None:
    _init_wiki()
    project = tmp_path / "proj"
    _seed_project_asset(project, "commands", "demo", "command.md", body=f"# c\n{SECRET}\n".encode())
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "promote", "demo", "--project", str(project)])

    assert result.exit_code == 1
    assert "AKIA1234567890ABCDEF" not in _combined(result)
    assert "Gate A" in _combined(result)


def test_cli_promote_missing_source_exit1(wiki_root: Path, tmp_path: Path) -> None:
    _init_wiki()
    runner = CliRunner()
    result = runner.invoke(
        wiki_group, ["skill", "promote", "nope", "--project", str(tmp_path / "proj")]
    )
    assert result.exit_code == 1
    assert "to promote" in _combined(result)


def test_cli_promote_absent_wiki_exit1(wiki_root: Path, tmp_path: Path) -> None:
    # Wiki not initialized.
    project = tmp_path / "proj"
    _seed_project_asset(project, "skills", "demo", "SKILL.md")
    runner = CliRunner()
    result = runner.invoke(wiki_group, ["skill", "promote", "demo", "--project", str(project)])
    assert result.exit_code == 1
