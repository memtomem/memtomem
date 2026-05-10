"""ADR-0011 PR-E generator signature regression tests.

Pins exact (generator, scope) → path mapping per runtime so the canonical
vs runtime mix-up that the earlier plan revision had cannot regress.
Default kwarg ``scope="project_shared"`` preserves pre-PR-E behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context.agents import (
    AGENT_GENERATORS,
    ClaudeAgentsGenerator,
    CodexAgentsGenerator,
    GeminiAgentsGenerator,
)
from memtomem.context.commands import (
    COMMAND_GENERATORS,
    ClaudeCommandsGenerator,
    GeminiCommandsGenerator,
)
from memtomem.context.skills import (
    SKILL_GENERATORS,
    ClaudeSkillsGenerator,
    CodexSkillsGenerator,
    GeminiSkillsGenerator,
)


# ---------------------------------------------------------------------------
# Default kwarg preserves pre-PR-E behavior (project_shared)
# ---------------------------------------------------------------------------


def test_default_kwarg_matches_project_shared_agents(tmp_path: Path) -> None:
    p = tmp_path / "proj"
    assert ClaudeAgentsGenerator().target_file(p, "foo") == ClaudeAgentsGenerator().target_file(
        p, "foo", scope="project_shared"
    )


def test_default_kwarg_matches_project_shared_skills(tmp_path: Path) -> None:
    p = tmp_path / "proj"
    assert ClaudeSkillsGenerator().target_dir(p, "skill1") == ClaudeSkillsGenerator().target_dir(
        p, "skill1", scope="project_shared"
    )


def test_default_kwarg_matches_project_shared_commands(tmp_path: Path) -> None:
    p = tmp_path / "proj"
    assert ClaudeCommandsGenerator().target_file(
        p, "cmd1"
    ) == ClaudeCommandsGenerator().target_file(p, "cmd1", scope="project_shared")


# ---------------------------------------------------------------------------
# Per-runtime concrete path pins — agents
# ---------------------------------------------------------------------------


def test_claude_agents_user_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out = ClaudeAgentsGenerator().target_file(Path("/ignored"), "foo", scope="user")
    assert out == (tmp_path / ".claude" / "agents" / "foo.md").resolve()


def test_claude_agents_project_shared(tmp_path: Path) -> None:
    out = ClaudeAgentsGenerator().target_file(tmp_path, "foo", scope="project_shared")
    assert out == (tmp_path / ".claude" / "agents" / "foo.md").resolve()


def test_claude_agents_project_local_returns_none(tmp_path: Path) -> None:
    assert ClaudeAgentsGenerator().target_file(tmp_path, "foo", scope="project_local") is None


def test_gemini_agents_user_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out = GeminiAgentsGenerator().target_file(Path("/ignored"), "foo", scope="user")
    assert out == (tmp_path / ".gemini" / "agents" / "foo.md").resolve()


def test_codex_agents_user_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out = CodexAgentsGenerator().target_file(Path("/ignored"), "foo", scope="user")
    assert out == (tmp_path / ".codex" / "agents" / "foo.toml").resolve()


def test_codex_agents_project_shared_uses_toml(tmp_path: Path) -> None:
    out = CodexAgentsGenerator().target_file(tmp_path, "foo", scope="project_shared")
    assert out == (tmp_path / ".codex" / "agents" / "foo.toml").resolve()


# ---------------------------------------------------------------------------
# Per-runtime concrete path pins — skills (directories, no file suffix)
# ---------------------------------------------------------------------------


def test_claude_skills_user_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out = ClaudeSkillsGenerator().target_dir(Path("/ignored"), "skill1", scope="user")
    assert out == (tmp_path / ".claude" / "skills" / "skill1").resolve()


def test_codex_skills_project_uses_dot_agents(tmp_path: Path) -> None:
    """Confirm codex skills project-scope is .agents/skills, NOT .codex/skills."""
    out = CodexSkillsGenerator().target_dir(tmp_path, "skill1", scope="project_shared")
    assert out == (tmp_path / ".agents" / "skills" / "skill1").resolve()


def test_gemini_skills_project_local_returns_none(tmp_path: Path) -> None:
    assert GeminiSkillsGenerator().target_dir(tmp_path, "skill1", scope="project_local") is None


# ---------------------------------------------------------------------------
# Per-runtime concrete path pins — commands
# ---------------------------------------------------------------------------


def test_claude_commands_user_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out = ClaudeCommandsGenerator().target_file(Path("/ignored"), "cmd1", scope="user")
    assert out == (tmp_path / ".claude" / "commands" / "cmd1.md").resolve()


def test_gemini_commands_uses_toml(tmp_path: Path) -> None:
    out = GeminiCommandsGenerator().target_file(tmp_path, "cmd1", scope="project_shared")
    assert out == (tmp_path / ".gemini" / "commands" / "cmd1.toml").resolve()


def test_claude_commands_project_local_returns_none(tmp_path: Path) -> None:
    assert ClaudeCommandsGenerator().target_file(tmp_path, "cmd1", scope="project_local") is None


# ---------------------------------------------------------------------------
# Registry sanity — every registered generator accepts the scope kwarg
# ---------------------------------------------------------------------------


def test_all_registered_agent_generators_accept_scope(tmp_path: Path) -> None:
    for name, gen in AGENT_GENERATORS.items():
        out = gen.target_file(tmp_path, "x", scope="project_shared")
        assert out is not None, f"{name}: project_shared should resolve to a path"
        assert gen.target_file(tmp_path, "x", scope="project_local") is None, (
            f"{name}: project_local should return None"
        )


def test_all_registered_skill_generators_accept_scope(tmp_path: Path) -> None:
    for name, gen in SKILL_GENERATORS.items():
        out = gen.target_dir(tmp_path, "x", scope="project_shared")
        assert out is not None, f"{name}: project_shared should resolve to a path"
        assert gen.target_dir(tmp_path, "x", scope="project_local") is None, (
            f"{name}: project_local should return None"
        )


def test_all_registered_command_generators_accept_scope(tmp_path: Path) -> None:
    for name, gen in COMMAND_GENERATORS.items():
        out = gen.target_file(tmp_path, "x", scope="project_shared")
        assert out is not None, f"{name}: project_shared should resolve to a path"
        assert gen.target_file(tmp_path, "x", scope="project_local") is None, (
            f"{name}: project_local should return None"
        )
