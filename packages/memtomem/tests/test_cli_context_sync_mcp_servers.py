"""CLI tests for the ``mm context sync --include=mcp-servers`` leg (#1311).

The engine (:func:`memtomem.context.mcp_servers.generate_all_mcp_servers` —
canonical-wins-per-name merge, the ``IN_SYNC`` no-op, the foreign-entry
mode policy, Gate A) is pinned by ``test_context_mcp_servers.py``. This file
covers what the CLI leg adds on top of that engine:

- the opt-in contract: ``--include=mcp-servers`` fans out, while a bare
  ``mm context sync`` (and any other ``--include``) never touches ``.mcp.json``
  — the regression guard the #1311 decision turns on;
- sync-only scoping: detect/init/generate/diff REJECT ``--include=mcp-servers``
  (the kind has a canonical→``.mcp.json`` fan-out but no extract/init/diff, so
  the shared ``_KNOWN_INCLUDES`` set is deliberately NOT widened);
- the ``--scope`` ignore note (mcp-servers is single-tier project_shared);
- a Gate A privacy hit surfaced as a red CLI error that aborts the leg, leaving
  no ``.mcp.json`` behind.

The ``--all-projects`` per-target confirm lives in
``test_cli_context_sync_all_projects.py`` (its registry harness).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context as context_group

_CLEAN = {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-postgres"],
    "env": {"PG_HOST": "localhost"},
}
#: AWS-key shape — caught by Gate A's env-block scan.
_SECRET = {"command": "npx", "env": {"AWS_ACCESS_KEY": "AKIA1234567890ABCDEF"}}


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root (``.git`` marker so ``_find_project_root`` resolves it)
    with an isolated HOME so the Gate A privacy audit stays hermetic."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    root = tmp_path / "proj"
    (root / ".memtomem").mkdir(parents=True)
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    return root


def _seed(root: Path, name: str = "pg", definition: dict | None = None) -> Path:
    store = root / ".memtomem" / "mcp-servers"
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{name}.json"
    path.write_text(
        json.dumps(definition if definition is not None else _CLEAN, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _run(args: list[str], **kw: object) -> object:
    return CliRunner().invoke(context_group, args, **kw)  # type: ignore[arg-type]


def test_include_mcp_servers_fans_into_mcp_json(project: Path) -> None:
    _seed(project)
    result = _run(["sync", "--include=mcp-servers"])
    assert result.exit_code == 0, result.output
    assert "MCP servers fan-out: 1" in result.output
    written = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert written["mcpServers"]["pg"]["command"] == "npx"


def test_canonical_wins_merge_preserves_foreign_entry(project: Path) -> None:
    _seed(project)
    (project / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"keepme": {"command": "stay"}, "pg": {"command": "OLD"}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    result = _run(["sync", "--include=mcp-servers"])
    assert result.exit_code == 0, result.output
    written = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    # canonical wins for its own name; the foreign entry is carried verbatim.
    assert written["mcpServers"]["pg"]["command"] == "npx"
    assert written["mcpServers"]["keepme"] == {"command": "stay"}


def test_rerun_is_in_sync_noop(project: Path) -> None:
    _seed(project)
    assert _run(["sync", "--include=mcp-servers"]).exit_code == 0
    before = (project / ".mcp.json").read_bytes()
    result = _run(["sync", "--include=mcp-servers"])
    assert result.exit_code == 0, result.output
    assert "already in sync" in result.output
    assert (project / ".mcp.json").read_bytes() == before


def test_bare_sync_never_touches_mcp_json(project: Path) -> None:
    """The opt-in regression guard: mcp-servers fans out ONLY on --include."""
    _seed(project)
    (project / ".memtomem" / "context.md").write_text("## Notes\n\nhi\n", encoding="utf-8")
    result = _run(["sync"])
    assert result.exit_code == 0, result.output
    assert not (project / ".mcp.json").exists()


def test_other_include_does_not_touch_mcp_json(project: Path) -> None:
    _seed(project)
    result = _run(["sync", "--include=skills"])
    assert result.exit_code == 0, result.output
    assert not (project / ".mcp.json").exists()


@pytest.mark.parametrize("subcommand", ["detect", "init", "generate", "diff"])
def test_non_sync_commands_reject_mcp_servers(project: Path, subcommand: str) -> None:
    """mcp-servers is a sync-only include token — the other four commands keep
    the 4-kind _KNOWN_INCLUDES and reject it at parse time."""
    _seed(project)
    result = _run([subcommand, "--include=mcp-servers"])
    assert result.exit_code != 0
    assert "mcp-servers" in result.output
    assert not (project / ".mcp.json").exists()


def test_scope_note_when_non_project_shared_still_fans_project_shared(project: Path) -> None:
    _seed(project)
    result = _run(["sync", "--include=mcp-servers", "--scope", "user"])
    assert result.exit_code == 0, result.output
    assert "does not apply to mcp-servers" in result.output
    # The ignored --scope does not block the fan-out; it lands project_shared.
    assert (project / ".mcp.json").exists()


def test_privacy_block_aborts_leg_and_writes_nothing(project: Path) -> None:
    _seed(project, definition=_SECRET)
    result = _run(["sync", "--include=mcp-servers"])
    assert result.exit_code != 0
    assert "Gate A" in result.output
    assert not (project / ".mcp.json").exists()
