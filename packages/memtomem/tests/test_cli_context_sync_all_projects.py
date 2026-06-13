"""CLI tests for ``mm context sync --all-projects`` (A-9 #1279, ADR-0025).

Single-project ``mm context sync`` behavior is pinned by the existing
suites (``test_context_sync_scope.py``, ``test_context_sync_empty_guard.py``,
…) — the ``_run_sync_legs`` extraction keeps that path byte-identical and
those suites are the parity pin. This file covers what the batch adds:

- preview table → confirm → serial execute → summary, ``--yes`` and the
  decline path, exit 1 on any failed project;
- skip rows (paused / missing / stale) with reasons, and the zero-eligible
  no-op exit 0 (cron safety);
- per-project failure isolation (privacy Gate A) — sibling still syncs;
- the project_shared tier pin: usage errors for other ``--scope`` values,
  and the settings leg ignoring a config-pinned ``hooks.target_scope =
  "user"`` (positive: project settings written; negative: HOME untouched —
  asserting only HOME would false-pass a no-op leg, Codex design-gate fold);
- the ``_find_project_root()`` discovery anchor: a run from a project
  SUBDIRECTORY targets the project root, never the subdir (Codex fold).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context as context_group
from memtomem.config import ContextGatewayConfig

from .helpers import set_home

_SKILL_BODY = "# Demo skill\n\nDo the demo thing.\n"

_AGENT_BODY = """---
name: reviewer
description: Code review agent
tools: [Read, Grep]
---
You are a code review agent.
"""

_SECRET_AGENT_BODY = """---
name: leaky
description: leaks a credential
---
key=AKIA1234567890ABCDEF
"""

_HOOK_RULE = {
    "matcher": "",
    "hooks": [{"type": "command", "command": "echo ok"}],
}


# ── Setup helpers ────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox HOME and scrub the env override that outranks config.json."""
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.delenv("MEMTOMEM_HOOKS__TARGET_SCOPE", raising=False)
    return home


@pytest.fixture
def known_projects_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI's ``ContextGatewayConfig()`` at a per-test registry.

    A real config instance (not a bare fake) because ``_projects_discover``
    also reads the two scan flags.
    """
    kp = tmp_path / "kp.json"
    monkeypatch.setattr(
        "memtomem.cli.context_cmd.ContextGatewayConfig",
        lambda: ContextGatewayConfig(
            known_projects_path=kp,
            experimental_claude_projects_scan=False,
        ),
    )
    return kp


def _seed_known_projects(path: Path, entries: list[tuple[Path, bool]]) -> None:
    doc = {
        "version": 1,
        "projects": [
            {"root": str(p), "added_at": "2026-01-01T00:00:00Z", "label": None, "enabled": en}
            for p, en in entries
        ],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


def _project(
    tmp_path: Path,
    name: str,
    *,
    store: bool = True,
    skill: bool = False,
    agent_body: str | None = None,
    settings: bool = False,
) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / ".claude").mkdir()
    if store:
        (root / ".memtomem").mkdir()
    if skill:
        skill_dir = root / ".memtomem" / "skills" / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_SKILL_BODY, encoding="utf-8")
    if agent_body is not None:
        agents = root / ".memtomem" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        name_line = [ln for ln in agent_body.splitlines() if ln.startswith("name:")][0]
        (agents / f"{name_line.split(':', 1)[1].strip()}.md").write_text(
            agent_body, encoding="utf-8"
        )
    if settings:
        (root / ".memtomem" / "settings.json").write_text(
            json.dumps({"hooks": {"PostToolUse": [_HOOK_RULE]}}),
            encoding="utf-8",
        )
    return root


def _runtime_skill(root: Path) -> Path:
    return root / ".claude" / "skills" / "demo-skill" / "SKILL.md"


# ── Flow: preview / confirm / execute / summary ──────────────────────────


def test_decline_at_confirm_writes_nothing(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _project(tmp_path, "proj-a", skill=True)
    b = _project(tmp_path, "proj-b", skill=True)
    _seed_known_projects(known_projects_path, [(a, True), (b, True)])
    monkeypatch.chdir(a)

    result = CliRunner().invoke(
        context_group, ["sync", "--all-projects", "--include", "skills"], input="n\n"
    )
    assert result.exit_code != 0
    assert "2 project scope(s) discovered:" in result.output
    assert "Sync 2 project(s)?" in result.output
    assert not _runtime_skill(a).exists()
    assert not _runtime_skill(b).exists()


def test_yes_executes_every_eligible_project(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _project(tmp_path, "proj-a", skill=True)
    b = _project(tmp_path, "proj-b", skill=True)
    _seed_known_projects(known_projects_path, [(a, True), (b, True)])
    monkeypatch.chdir(a)  # cwd row coalesces with a's registry entry

    result = CliRunner().invoke(
        context_group, ["sync", "--all-projects", "--include", "skills", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert _runtime_skill(a).read_text(encoding="utf-8") == _SKILL_BODY
    assert _runtime_skill(b).read_text(encoding="utf-8") == _SKILL_BODY
    # The batch memory-leg note (a registered project without context.md
    # is not a failure) and the update-all-style summary.
    assert "missing — skipping project memory" in result.output.replace("context.md ", "")
    assert "Summary: 2 synced, 0 failed, 0 skipped." in result.output


def _seed_mcp(root: Path) -> None:
    store = root / ".memtomem" / "mcp-servers"
    store.mkdir(parents=True, exist_ok=True)
    (store / "pg.json").write_text(
        json.dumps({"command": "npx", "args": ["-y", "server-pg"]}, indent=2) + "\n",
        encoding="utf-8",
    )


def test_all_projects_mcp_servers_declined_skips_write(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count-only "Sync N project(s)?" gate never discloses the .mcp.json
    rewrite, so the mcp-servers leg confirms per target in a batch; declining
    skips just that write and the project still succeeds (#1311)."""
    a = _project(tmp_path, "proj-a")
    _seed_mcp(a)
    _seed_known_projects(known_projects_path, [(a, True)])
    monkeypatch.chdir(a)

    # "y" to the batch "Sync 1 project(s)?" gate, then "n" to the per-target
    # mcp-servers fan-out confirm.
    result = CliRunner().invoke(
        context_group, ["sync", "--all-projects", "--include", "mcp-servers"], input="y\nn\n"
    )
    assert result.exit_code == 0, result.output
    assert "Skipped mcp-servers sync (declined)." in result.output
    assert not (a / ".mcp.json").exists()


def test_all_projects_mcp_servers_yes_fans_out_without_prompt(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _project(tmp_path, "proj-a")
    _seed_mcp(a)
    _seed_known_projects(known_projects_path, [(a, True)])
    monkeypatch.chdir(a)

    result = CliRunner().invoke(
        context_group, ["sync", "--all-projects", "--include", "mcp-servers", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "MCP servers fan-out: 1" in result.output
    written = json.loads((a / ".mcp.json").read_text(encoding="utf-8"))
    assert written["mcpServers"]["pg"]["command"] == "npx"


def test_subdirectory_run_anchors_at_project_root(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex design-gate fold: discovery anchors at ``_find_project_root()``
    — from a subdir the PARENT project is the cwd scope; nothing is ever
    created under the subdir (raw ``Path.cwd()`` would make the subdir a
    stale scope and skip the project entirely)."""
    a = _project(tmp_path, "proj-a", skill=True)
    (a / "pyproject.toml").write_text('[project]\nname = "a"\nversion = "0"\n', encoding="utf-8")
    subdir = a / "src" / "pkg"
    subdir.mkdir(parents=True)
    _seed_known_projects(known_projects_path, [])
    monkeypatch.chdir(subdir)

    result = CliRunner().invoke(
        context_group, ["sync", "--all-projects", "--include", "skills", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert f"({a})" in result.output  # the preview row names the project root
    assert "Summary: 1 synced, 0 failed, 0 skipped." in result.output
    assert _runtime_skill(a).exists()
    assert not (subdir / ".claude").exists()
    assert not (subdir / ".memtomem").exists()


def test_skip_rows_paused_missing_stale(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _project(tmp_path, "proj-a", skill=True)
    paused = _project(tmp_path, "paused", skill=True)
    gone = _project(tmp_path, "gone")
    stale = _project(tmp_path, "stale", store=False)
    _seed_known_projects(known_projects_path, [(paused, False), (gone, True), (stale, True)])
    shutil.rmtree(gone)
    monkeypatch.chdir(a)

    result = CliRunner().invoke(
        context_group, ["sync", "--all-projects", "--include", "skills", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "paused — `mm context projects resume" in result.output
    assert "missing — root no longer exists" in result.output
    assert "stale — no .memtomem/ store" in result.output
    assert "Summary: 1 synced, 0 failed, 3 skipped." in result.output
    assert _runtime_skill(a).exists()
    assert not _runtime_skill(paused).exists()


def test_zero_eligible_projects_is_noop_success(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cron safety (the ``_run_update_all`` precedent): an empty registry
    plus a stale cwd exits 0 with an info line, no prompt."""
    neutral = _project(tmp_path, "neutral", store=False)
    _seed_known_projects(known_projects_path, [])
    monkeypatch.chdir(neutral)

    result = CliRunner().invoke(context_group, ["sync", "--all-projects"])
    assert result.exit_code == 0, result.output
    assert "No projects are eligible for sync; nothing to do." in result.output
    assert "Sync " not in result.output  # never reached the confirm prompt


# ── Failure isolation ────────────────────────────────────────────────────


def test_privacy_block_fails_project_but_not_batch(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _project(tmp_path, "proj-a", agent_body=_AGENT_BODY)
    leaky = _project(tmp_path, "leaky", agent_body=_SECRET_AGENT_BODY)
    _seed_known_projects(known_projects_path, [(leaky, True)])
    monkeypatch.chdir(a)

    result = CliRunner().invoke(
        context_group, ["sync", "--all-projects", "--include", "agents", "--yes"]
    )
    assert result.exit_code == 1, result.output
    assert (a / ".claude" / "agents" / "reviewer.md").exists()
    assert f"✗ {leaky}" in result.output
    assert "Summary: 1 synced, 1 failed, 0 skipped." in result.output
    # The blocked secret never reaches the runtime tree or the output.
    assert not (leaky / ".claude" / "agents" / "leaky.md").exists()
    assert "AKIA1234567890ABCDEF" not in result.output


# ── Tier pin (ADR-0025 §4) ───────────────────────────────────────────────


@pytest.mark.parametrize("tier", ["user", "project_local"])
def test_scope_other_than_project_shared_is_usage_error(
    tier: str,
    tmp_path: Path,
    fake_home: Path,
    known_projects_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = _project(tmp_path, "proj-a")
    _seed_known_projects(known_projects_path, [])
    monkeypatch.chdir(a)

    result = CliRunner().invoke(context_group, ["sync", "--all-projects", "--scope", tier])
    assert result.exit_code == 2, result.output
    assert "--all-projects syncs the project_shared tier only" in result.output


def test_explicit_project_shared_scope_is_accepted(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _project(tmp_path, "proj-a", skill=True)
    _seed_known_projects(known_projects_path, [])
    monkeypatch.chdir(a)

    result = CliRunner().invoke(
        context_group,
        ["sync", "--all-projects", "--scope", "project_shared", "--include", "skills", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert _runtime_skill(a).exists()


def _arm_user_scope_config(fake_home: Path) -> None:
    """Pin ``hooks.target_scope = user`` in the (sandboxed) config.json."""
    cfg_dir = fake_home / ".memtomem"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"hooks": {"target_scope": "user"}}), encoding="utf-8"
    )


def test_settings_leg_pinned_to_project_shared_despite_user_config(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0025 §4 + Codex design-gate fold: with ``hooks.target_scope =
    "user"`` pinned in config, the batch settings leg must still write the
    PROJECT settings files (positive — a HOME-only assertion would
    false-pass a no-op leg) and must not touch the host ``~/.claude``
    (negative — the N×-host-write hazard)."""
    _arm_user_scope_config(fake_home)
    a = _project(tmp_path, "proj-a", settings=True)
    b = _project(tmp_path, "proj-b", settings=True)
    _seed_known_projects(known_projects_path, [(b, True)])
    monkeypatch.chdir(a)

    result = CliRunner().invoke(
        context_group, ["sync", "--all-projects", "--include", "settings", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert (a / ".claude" / "settings.json").is_file()
    assert (b / ".claude" / "settings.json").is_file()
    assert not (fake_home / ".claude" / "settings.json").exists()
    assert "Summary: 2 synced, 0 failed, 0 skipped." in result.output


def test_user_config_control_single_sync_does_write_home(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the pin test above: the SAME config arms a real hazard —
    a plain single-project sync resolves the user tier from config and
    writes the host file (with ``--yes``). Proves the batch assertion is
    not vacuous."""
    _arm_user_scope_config(fake_home)
    a = _project(tmp_path, "proj-a", settings=True)
    _seed_known_projects(known_projects_path, [])
    monkeypatch.chdir(a)

    result = CliRunner().invoke(context_group, ["sync", "--include", "settings", "--yes"])
    assert result.exit_code == 0, result.output
    assert (fake_home / ".claude" / "settings.json").is_file()


# ── Single-project extraction parity ─────────────────────────────────────


def test_single_sync_missing_context_prints_no_false_success(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex impl-review regression: plain ``mm context sync`` in a project
    with no ``context.md`` and no ``--include`` refuses with the init hint
    and must NOT also print ``Synced.`` — the extracted ``_run_sync_legs``
    early-return has to keep suppressing the caller's success line."""
    a = _project(tmp_path, "proj-a")  # .memtomem exists, no context.md
    _seed_known_projects(known_projects_path, [])
    monkeypatch.chdir(a)

    result = CliRunner().invoke(context_group, ["sync"])
    assert result.exit_code == 0, result.output
    assert "not found. Run 'mm context init' first." in result.output
    assert "Synced." not in result.output


# ── Option validation ────────────────────────────────────────────────────


def test_unknown_include_rejected_before_discovery(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _project(tmp_path, "proj-a")
    _seed_known_projects(known_projects_path, [])
    monkeypatch.chdir(a)

    result = CliRunner().invoke(context_group, ["sync", "--all-projects", "--include", "bogus"])
    assert result.exit_code == 2, result.output
    assert "Unknown --include value 'bogus'" in result.output
