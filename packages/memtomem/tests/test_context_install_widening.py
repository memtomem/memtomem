"""Tests for ADR-0008 PR-C: ``mm context install`` widened to agents/commands.

PR-B shipped install_skill end-to-end. PR-C widens the public surface to
``install_agent`` and ``install_command`` and updates the CLI ``Choice`` so
that all three kinds flow through the same install pipeline. Wiki layout
for new agents/commands is the directory form per ADR-0008
(``agents/<name>/agent.md``, ``commands/<name>/command.md``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context as context_group
from memtomem.context.install import (
    install_agent,
    install_command,
    install_skill,
)
from memtomem.context.lockfile import Lockfile
from memtomem.wiki.store import WikiStore


# ── helpers ──────────────────────────────────────────────────────────────


def _initialized_wiki() -> WikiStore:
    store = WikiStore.at_default()
    store.init()
    return store


def _git_commit(wiki_root_path: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", message],
        check=True,
        capture_output=True,
    )


def _seed_agent(wiki_root_path: Path, name: str, body: bytes) -> None:
    agent_dir = wiki_root_path / "agents" / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.md").write_bytes(body)
    _git_commit(wiki_root_path, f"add agent {name}")


def _seed_command(wiki_root_path: Path, name: str, body: bytes) -> None:
    cmd_dir = wiki_root_path / "commands" / name
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "command.md").write_bytes(body)
    _git_commit(wiki_root_path, f"add command {name}")


@pytest.fixture
def project_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.chdir(project)
    return project


# ── install_agent / install_command happy paths ──────────────────────────


def test_install_agent_dir_layout(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki()
    body = b"---\nname: bar\ndescription: test agent\n---\n\nhello body\n"
    _seed_agent(wiki_root, "bar", body)
    project = tmp_path

    result = install_agent(project, "bar")

    assert result.asset_type == "agents"
    assert result.name == "bar"
    dest = project / ".memtomem" / "agents" / "bar"
    assert (dest / "agent.md").read_bytes() == body
    assert result.dest == dest


def test_install_command_dir_layout(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki()
    body = b"---\ndescription: hi\n---\n\nrun $ARGUMENTS\n"
    _seed_command(wiki_root, "greet", body)
    project = tmp_path

    result = install_command(project, "greet")

    assert result.asset_type == "commands"
    assert result.name == "greet"
    dest = project / ".memtomem" / "commands" / "greet"
    assert (dest / "command.md").read_bytes() == body


def test_install_records_lockfile_section_per_kind(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki()
    (wiki_root / "skills" / "alpha").mkdir(parents=True)
    (wiki_root / "skills" / "alpha" / "SKILL.md").write_bytes(b"# alpha\n")
    _git_commit(wiki_root, "alpha skill")
    _seed_agent(wiki_root, "bar", b"---\nname: bar\ndescription: x\n---\nbody\n")
    _seed_command(wiki_root, "baz", b"---\ndescription: y\n---\nrun\n")
    project = tmp_path

    install_skill(project, "alpha")
    install_agent(project, "bar")
    install_command(project, "baz")

    lock_path = project / ".memtomem" / "lock.json"
    lock_doc = json.loads(lock_path.read_text())
    assert "alpha" in lock_doc["skills"]
    assert "bar" in lock_doc["agents"]
    assert "baz" in lock_doc["commands"]
    # Lockfile reads also work via the module API.
    lock = Lockfile.at(project)
    assert lock.read_entry("skills", "alpha") is not None
    assert lock.read_entry("agents", "bar") is not None
    assert lock.read_entry("commands", "baz") is not None


# ── CLI dispatch ────────────────────────────────────────────────────────


def test_cli_install_agent_success(
    wiki_root: Path,
    project_cwd: Path,
) -> None:
    _initialized_wiki()
    _seed_agent(wiki_root, "bar", b"---\nname: bar\ndescription: x\n---\nbody\n")
    runner = CliRunner()

    result = runner.invoke(context_group, ["install", "agent", "bar"])

    assert result.exit_code == 0, result.output
    assert "agents/bar" in result.output
    assert (project_cwd / ".memtomem" / "agents" / "bar" / "agent.md").is_file()


def test_cli_install_command_success(
    wiki_root: Path,
    project_cwd: Path,
) -> None:
    _initialized_wiki()
    _seed_command(wiki_root, "greet", b"---\ndescription: hi\n---\nrun\n")
    runner = CliRunner()

    result = runner.invoke(context_group, ["install", "command", "greet"])

    assert result.exit_code == 0, result.output
    assert "commands/greet" in result.output
    assert (project_cwd / ".memtomem" / "commands" / "greet" / "command.md").is_file()
