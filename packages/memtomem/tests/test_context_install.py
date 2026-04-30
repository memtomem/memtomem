"""Tests for ``memtomem.context.install`` — wiki-asset install pipeline.

Covers ADR-0008 PR-B: Invariant 1 (copytree snapshot), Invariant 3
(precise wiki-not-found error), and the OR-refusal forward-protection of
Invariant 2 (refuse-on-conflict instead of silent clobber).
"""

from __future__ import annotations

import json
import multiprocessing as mp
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context as context_group
from memtomem.context._names import InvalidNameError
from memtomem.context.install import (
    AlreadyInstalledError,
    AssetNotFoundError,
    install_skill,
)
from memtomem.context.lockfile import LOCKFILE_VERSION, Lockfile
from memtomem.wiki.store import WikiNotFoundError, WikiStore


# ── helpers ──────────────────────────────────────────────────────────────


def _seed_skill(wiki_root_path: Path, name: str, files: dict[str, bytes]) -> None:
    """Drop a skill into an initialized wiki and commit."""
    skill_dir = wiki_root_path / "skills" / name
    skill_dir.mkdir(parents=True)
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


def _initialized_wiki(wiki_root_path: Path) -> WikiStore:
    store = WikiStore.at_default()
    store.init()
    return store


# ── install_skill: happy paths ───────────────────────────────────────────


def test_install_skill_copies_tree(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki(wiki_root)
    _seed_skill(
        wiki_root,
        "foo",
        {
            "SKILL.md": b"# foo skill\n",
            "scripts/run.sh": b"#!/bin/bash\necho hi\n",
            "overrides/claude.md": b"claude-only override\n",
        },
    )
    project = tmp_path

    result = install_skill(project, "foo")

    assert result.asset_type == "skills"
    assert result.name == "foo"
    assert result.files_written == 3
    dest = project / ".memtomem" / "skills" / "foo"
    assert (dest / "SKILL.md").read_bytes() == b"# foo skill\n"
    assert (dest / "scripts" / "run.sh").read_bytes() == b"#!/bin/bash\necho hi\n"
    assert (dest / "overrides" / "claude.md").read_bytes() == b"claude-only override\n"


def test_install_records_lockfile_entry(wiki_root: Path, tmp_path: Path) -> None:
    store = _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    project = tmp_path

    result = install_skill(project, "foo")

    expected_commit = store.current_commit()
    assert result.wiki_commit == expected_commit
    assert len(result.wiki_commit) == 40

    # ISO8601-Z with microseconds: YYYY-MM-DDTHH:MM:SS.ffffffZ
    assert result.installed_at.endswith("Z")
    assert "." in result.installed_at  # microsecond separator
    assert "T" in result.installed_at

    lock_doc = json.loads((project / ".memtomem" / "lock.json").read_text())
    assert lock_doc["version"] == LOCKFILE_VERSION
    assert lock_doc["skills"]["foo"]["wiki_commit"] == expected_commit
    assert lock_doc["skills"]["foo"]["installed_at"] == result.installed_at


def test_install_skips_dotgit_in_source(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki(wiki_root)
    _seed_skill(
        wiki_root,
        "foo",
        {
            "SKILL.md": b"x",
            ".git/HEAD": b"ref: refs/heads/main\n",  # synthetic — won't really be added by git
        },
    )
    project = tmp_path

    install_skill(project, "foo")

    dest = project / ".memtomem" / "skills" / "foo"
    assert (dest / "SKILL.md").is_file()
    assert not (dest / ".git").exists()


def test_install_skips_dsstore_and_pycache(wiki_root: Path, tmp_path: Path) -> None:
    """COPY_SKIP_NAMES: macOS Finder + Python bytecode side-effects don't propagate."""
    _initialized_wiki(wiki_root)
    _seed_skill(
        wiki_root,
        "foo",
        {
            "SKILL.md": b"x",
            ".DS_Store": b"\x00\x00\x00\x00",
            "__pycache__/foo.cpython-312.pyc": b"\x00\x00",
        },
    )
    project = tmp_path

    install_skill(project, "foo")

    dest = project / ".memtomem" / "skills" / "foo"
    assert (dest / "SKILL.md").is_file()
    assert not (dest / ".DS_Store").exists()
    assert not (dest / "__pycache__").exists()


def test_install_skips_symlinks_in_source(
    wiki_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """copy_tree_atomic refuses to dereference symlinks — would silently
    leak out-of-tree bytes (e.g., /etc/passwd) into the project otherwise."""
    _initialized_wiki(wiki_root)
    skill_dir = wiki_root / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("real", encoding="utf-8")
    # Dangling symlink — entry.is_symlink() fires regardless of target validity.
    (skill_dir / "danger.md").symlink_to("/nonexistent/target")
    subprocess.run(["git", "-C", str(wiki_root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root), "commit", "-m", "add foo with symlink"],
        check=True,
        capture_output=True,
    )
    project = tmp_path

    with caplog.at_level("WARNING", logger="memtomem.context._atomic"):
        install_skill(project, "foo")

    dest = project / ".memtomem" / "skills" / "foo"
    assert (dest / "SKILL.md").is_file()
    assert not (dest / "danger.md").exists()
    assert any("skipping symlink" in r.message for r in caplog.records)


def test_install_files_written_with_default_mode(wiki_root: Path, tmp_path: Path) -> None:
    """Asset content lands at 0o644 (readable by other tools), not at the
    0o600 atomic_write_bytes default reserved for state files."""
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    project = tmp_path

    install_skill(project, "foo")

    skill_md = project / ".memtomem" / "skills" / "foo" / "SKILL.md"
    # Owner-readable + group/other-readable; no write for group/other.
    assert (skill_md.stat().st_mode & 0o777) == 0o644


# ── install_skill: failure paths ─────────────────────────────────────────


def test_install_project_root_missing(wiki_root: Path, tmp_path: Path) -> None:
    """A typo'd project root errors loudly instead of silently creating it."""
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    missing = tmp_path / "nonexistent"

    with pytest.raises(FileNotFoundError, match="project root does not exist"):
        install_skill(missing, "foo")


def test_install_wiki_missing_invariant3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_identity: None,  # noqa: ARG001
) -> None:
    """Invariant 3: precise message including path and `mm wiki init`."""
    monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(tmp_path / "no-wiki"))
    project = tmp_path

    with pytest.raises(WikiNotFoundError) as excinfo:
        install_skill(project, "foo")
    assert "wiki not found at" in str(excinfo.value)
    assert "mm wiki init" in str(excinfo.value)


def test_install_asset_missing(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki(wiki_root)
    project = tmp_path
    with pytest.raises(AssetNotFoundError, match="skills/nope"):
        install_skill(project, "nope")


def test_install_refuses_when_lockfile_and_dest_present(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    project = tmp_path

    install_skill(project, "foo")

    with pytest.raises(AlreadyInstalledError) as excinfo:
        install_skill(project, "foo")
    msg = str(excinfo.value)
    assert "lockfile_entry=yes" in msg
    assert "dest=yes" in msg


def test_install_refuses_when_only_lockfile_present(wiki_root: Path, tmp_path: Path) -> None:
    """The OR-not-AND case: user wiped dest but left lockfile orphaned."""
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    project = tmp_path

    install_skill(project, "foo")
    shutil.rmtree(project / ".memtomem" / "skills" / "foo")

    with pytest.raises(AlreadyInstalledError) as excinfo:
        install_skill(project, "foo")
    msg = str(excinfo.value)
    assert "lockfile_entry=yes" in msg
    assert "dest=no" in msg


def test_install_refuses_when_only_dest_present(wiki_root: Path, tmp_path: Path) -> None:
    """Lockfile damaged (or external copy) but dest exists."""
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    project = tmp_path
    pre = project / ".memtomem" / "skills" / "foo"
    pre.mkdir(parents=True)
    (pre / "stray.txt").write_text("hand-placed", encoding="utf-8")

    with pytest.raises(AlreadyInstalledError) as excinfo:
        install_skill(project, "foo")
    msg = str(excinfo.value)
    assert "lockfile_entry=no" in msg
    assert "dest=yes" in msg
    # Hand-placed file must not be clobbered.
    assert (pre / "stray.txt").read_text() == "hand-placed"


def test_install_invalid_name(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki(wiki_root)
    project = tmp_path
    with pytest.raises(InvalidNameError):
        install_skill(project, "../escape")


# ── concurrency ──────────────────────────────────────────────────────────


def _install_worker(wiki_path_str: str, project_str: str, name: str) -> None:
    """Subprocess body — one install per worker, distinct skill names."""
    import os

    os.environ["MEMTOMEM_WIKI_PATH"] = wiki_path_str
    install_skill(Path(project_str), name)


def test_install_two_skills_concurrent(wiki_root: Path, tmp_path: Path) -> None:
    """Two installers, distinct skill names, share one lockfile."""
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    _seed_skill(wiki_root, "bar", {"SKILL.md": b"y"})
    project = tmp_path

    ctx = mp.get_context("spawn")
    p1 = ctx.Process(target=_install_worker, args=(str(wiki_root), str(project), "foo"))
    p2 = ctx.Process(target=_install_worker, args=(str(wiki_root), str(project), "bar"))
    p1.start()
    p2.start()
    p1.join(timeout=60)
    p2.join(timeout=60)
    assert p1.exitcode == 0, "installer 1 crashed"
    assert p2.exitcode == 0, "installer 2 crashed"

    lock_doc = json.loads((project / ".memtomem" / "lock.json").read_text())
    assert "foo" in lock_doc["skills"]
    assert "bar" in lock_doc["skills"]
    assert (project / ".memtomem" / "skills" / "foo" / "SKILL.md").read_bytes() == b"x"
    assert (project / ".memtomem" / "skills" / "bar" / "SKILL.md").read_bytes() == b"y"


# ── CLI ──────────────────────────────────────────────────────────────────


@pytest.fixture
def project_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Tmp project root (with a sentinel ``.git``) wired as cwd so
    ``_find_project_root`` resolves there. Uses a subdirectory of
    ``tmp_path`` so ``_find_project_root`` doesn't accidentally walk up
    into the test runner's ``tmp_path`` parent and find an unrelated
    ``.git``/``pyproject.toml`` first."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.chdir(project)
    return project


def test_cli_install_success(
    wiki_root: Path,
    project_cwd: Path,
) -> None:
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "hello", {"SKILL.md": b"# hello\n"})

    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "skill", "hello"])

    assert result.exit_code == 0, result.output
    assert "Installed skills/hello" in result.output
    assert (project_cwd / ".memtomem" / "skills" / "hello" / "SKILL.md").is_file()
    assert (project_cwd / ".memtomem" / "lock.json").is_file()


def test_cli_install_wiki_missing_message(
    monkeypatch: pytest.MonkeyPatch,
    project_cwd: Path,
    tmp_path: Path,
    git_identity: None,  # noqa: ARG001
) -> None:
    monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(tmp_path / "no-wiki"))
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "skill", "anything"])

    assert result.exit_code != 0
    assert "wiki not found at" in result.output


def test_cli_install_rejects_unknown_type(project_cwd: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "agent", "foo"])
    # PR-B: only "skill" is an allowed type; PR-C widens.
    assert result.exit_code != 0
    assert "agent" in result.output  # click usage error mentions the bad value


def test_cli_install_already_installed_classified_message(
    wiki_root: Path,
    project_cwd: Path,
) -> None:
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "hello", {"SKILL.md": b"# hello\n"})

    runner = CliRunner()
    runner.invoke(context_group, ["install", "skill", "hello"])
    result = runner.invoke(context_group, ["install", "skill", "hello"])

    assert result.exit_code != 0
    assert "lockfile_entry=yes" in result.output
    assert "dest=yes" in result.output


# ── Lockfile assertions about the live install ──────────────────────────


def test_lockfile_contains_only_two_keys_per_entry(wiki_root: Path, tmp_path: Path) -> None:
    """Schema discipline: install writes exactly the two mandated keys."""
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    project = tmp_path
    install_skill(project, "foo")

    lock = Lockfile.at(project)
    entry = lock.read_entry("skills", "foo")
    assert entry is not None
    assert set(entry) == {"wiki_commit", "installed_at"}
