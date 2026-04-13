"""Tests for context/agents.py — canonical ⇄ runtime sub-agent fan-out."""

import shutil
import tomllib

import pytest

from memtomem.context.agents import (
    AGENT_GENERATORS,
    CANONICAL_AGENT_ROOT,
    AgentParseError,
    StrictDropError,
    diff_agents,
    extract_agents_to_canonical,
    generate_all_agents,
    list_canonical_agents,
    parse_canonical_agent,
)
from memtomem.context.detector import detect_agent_dirs

SAMPLE_FULL_AGENT = """---
name: code-reviewer
description: Reviews staged code for quality
tools: [Read, Grep, Glob]
model: sonnet
skills: [code-review]
isolation: worktree
kind: reviewer
temperature: 0.2
---

You are a meticulous code reviewer.
Respond with a prioritized punch list.
"""

SAMPLE_MINIMAL_AGENT = """---
name: helper
description: Generic helper
---

Help with things.
"""


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    """Redirect HOME so Codex TOML writes don't touch the real ``~/.codex/agents/``."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))  # Windows safety (no-op on macOS)
    return fake_home


def _make_canonical_agent(project_root, name, body=SAMPLE_FULL_AGENT):
    root = project_root / CANONICAL_AGENT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.md"
    path.write_text(body)
    return path


class TestParseCanonicalAgent:
    def test_parses_all_fields(self, tmp_path):
        p = _make_canonical_agent(tmp_path, "code-reviewer", SAMPLE_FULL_AGENT)
        agent = parse_canonical_agent(p)
        assert agent.name == "code-reviewer"
        assert "quality" in agent.description
        assert agent.tools == ["Read", "Grep", "Glob"]
        assert agent.model == "sonnet"
        assert agent.skills == ["code-review"]
        assert agent.isolation == "worktree"
        assert agent.kind == "reviewer"
        assert agent.temperature == 0.2
        assert "meticulous code reviewer" in agent.body

    def test_parses_minimal(self, tmp_path):
        p = _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        agent = parse_canonical_agent(p)
        assert agent.name == "helper"
        assert agent.description == "Generic helper"
        assert agent.tools == []
        assert agent.model is None
        assert agent.skills == []
        assert agent.isolation is None
        assert agent.kind is None
        assert agent.temperature is None

    def test_missing_frontmatter_raises(self, tmp_path):
        p = tmp_path / "bad.md"
        p.write_text("no frontmatter at all")
        with pytest.raises(AgentParseError):
            parse_canonical_agent(p)

    def test_block_list_syntax(self, tmp_path):
        p = tmp_path / "blocky.md"
        p.write_text(
            "---\n"
            "name: blocky\n"
            "description: Uses block list for tools\n"
            "tools:\n"
            "  - Read\n"
            "  - Grep\n"
            "---\n\n"
            "body\n"
        )
        agent = parse_canonical_agent(p)
        assert agent.tools == ["Read", "Grep"]


class TestListCanonicalAgents:
    def test_empty(self, tmp_path):
        assert list_canonical_agents(tmp_path) == []

    def test_sorted(self, tmp_path):
        _make_canonical_agent(
            tmp_path, "zeta", SAMPLE_MINIMAL_AGENT.replace("helper", "zeta")
        )
        _make_canonical_agent(
            tmp_path, "alpha", SAMPLE_MINIMAL_AGENT.replace("helper", "alpha")
        )
        names = [p.stem for p in list_canonical_agents(tmp_path)]
        assert names == ["alpha", "zeta"]


class TestClaudeRendering:
    def test_passes_through_all_fields(self, tmp_path):
        _make_canonical_agent(tmp_path, "full", SAMPLE_FULL_AGENT)
        generate_all_agents(tmp_path, runtimes=["claude_agents"])
        out = (tmp_path / ".claude/agents/code-reviewer.md").read_text()
        assert "name: code-reviewer" in out
        assert "tools: [Read, Grep, Glob]" in out
        assert "model: sonnet" in out
        assert "skills: [code-review]" in out
        assert "isolation: worktree" in out
        assert "meticulous code reviewer" in out

    def test_drops_gemini_fields(self, tmp_path):
        _make_canonical_agent(tmp_path, "full", SAMPLE_FULL_AGENT)
        result = generate_all_agents(tmp_path, runtimes=["claude_agents"])
        assert result.dropped
        runtime, agent_name, fields = result.dropped[0]
        assert runtime == "claude_agents"
        assert "kind" in fields
        assert "temperature" in fields


class TestGeminiRendering:
    def test_passes_through_gemini_fields(self, tmp_path):
        _make_canonical_agent(tmp_path, "full", SAMPLE_FULL_AGENT)
        generate_all_agents(tmp_path, runtimes=["gemini_agents"])
        out = (tmp_path / ".gemini/agents/code-reviewer.md").read_text()
        assert "kind: reviewer" in out
        assert "temperature: 0.2" in out
        assert "tools: [Read, Grep, Glob]" in out
        assert "skills:" not in out  # dropped
        assert "isolation:" not in out  # dropped

    def test_drops_claude_fields(self, tmp_path):
        _make_canonical_agent(tmp_path, "full", SAMPLE_FULL_AGENT)
        result = generate_all_agents(tmp_path, runtimes=["gemini_agents"])
        assert result.dropped
        fields = result.dropped[0][2]
        assert "skills" in fields
        assert "isolation" in fields


class TestCodexRendering:
    def test_writes_valid_toml(self, tmp_path, codex_home):
        _make_canonical_agent(tmp_path, "full", SAMPLE_FULL_AGENT)
        generate_all_agents(tmp_path, runtimes=["codex_agents"])
        toml_path = codex_home / ".codex/agents/code-reviewer.toml"
        assert toml_path.is_file()
        parsed = tomllib.loads(toml_path.read_text())
        assert parsed["name"] == "code-reviewer"
        assert "quality" in parsed["description"]
        assert "meticulous" in parsed["developer_instructions"]
        assert parsed["model"] == "sonnet"

    def test_drops_tools_and_claude_specific(self, tmp_path, codex_home):
        _make_canonical_agent(tmp_path, "full", SAMPLE_FULL_AGENT)
        result = generate_all_agents(tmp_path, runtimes=["codex_agents"])
        fields = result.dropped[0][2]
        assert "tools" in fields
        assert "skills" in fields
        assert "isolation" in fields
        assert "kind" in fields
        assert "temperature" in fields

    def test_minimal_agent_has_no_dropped_fields(self, tmp_path, codex_home):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        result = generate_all_agents(tmp_path, runtimes=["codex_agents"])
        assert result.dropped == []
        toml_path = codex_home / ".codex/agents/helper.toml"
        parsed = tomllib.loads(toml_path.read_text())
        assert parsed["name"] == "helper"
        assert parsed["description"] == "Generic helper"

    def test_multiline_body_roundtrips_through_tomllib(self, tmp_path, codex_home):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        generate_all_agents(tmp_path, runtimes=["codex_agents"])
        toml_path = codex_home / ".codex/agents/helper.toml"
        parsed = tomllib.loads(toml_path.read_text())
        assert "Help with things." in parsed["developer_instructions"]


class TestGenerateAllAgents:
    def test_fans_out_to_all_three_runtimes(self, tmp_path, codex_home):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        result = generate_all_agents(tmp_path)
        assert len(result.generated) == 3
        assert (tmp_path / ".claude/agents/helper.md").is_file()
        assert (tmp_path / ".gemini/agents/helper.md").is_file()
        assert (codex_home / ".codex/agents/helper.toml").is_file()

    def test_no_canonical_no_op(self, tmp_path):
        result = generate_all_agents(tmp_path)
        assert result.generated == []
        assert result.skipped == [("<all>", "no canonical agents")]

    def test_registry_contents(self):
        assert "claude_agents" in AGENT_GENERATORS
        assert "gemini_agents" in AGENT_GENERATORS
        assert "codex_agents" in AGENT_GENERATORS

    def test_unknown_runtime_skipped(self, tmp_path):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        result = generate_all_agents(tmp_path, runtimes=["claude_agents", "nope"])
        assert ("nope", "unknown runtime") in result.skipped


class TestStrictMode:
    def test_strict_raises_on_dropped_fields(self, tmp_path):
        _make_canonical_agent(tmp_path, "full", SAMPLE_FULL_AGENT)
        with pytest.raises(StrictDropError):
            generate_all_agents(tmp_path, runtimes=["claude_agents"], strict=True)

    def test_strict_passes_with_minimal_agent(self, tmp_path):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        result = generate_all_agents(
            tmp_path,
            runtimes=["claude_agents", "gemini_agents"],
            strict=True,
        )
        assert len(result.generated) == 2


class TestExtractAgentsToCanonical:
    def test_imports_claude_agent(self, tmp_path):
        claude_dir = tmp_path / ".claude/agents"
        claude_dir.mkdir(parents=True)
        (claude_dir / "helper.md").write_text(SAMPLE_MINIMAL_AGENT)
        result = extract_agents_to_canonical(tmp_path)
        assert len(result.imported) == 1
        assert (tmp_path / CANONICAL_AGENT_ROOT / "helper.md").is_file()
        assert result.skipped == []

    def test_dedup_across_runtimes(self, tmp_path):
        for runtime in (".claude/agents", ".gemini/agents"):
            d = tmp_path / runtime
            d.mkdir(parents=True)
            (d / "helper.md").write_text(SAMPLE_MINIMAL_AGENT)
        result = extract_agents_to_canonical(tmp_path)
        assert len(result.imported) == 1
        # Gemini copy was skipped because Claude already imported it.
        assert len(result.skipped) == 1
        assert result.skipped[0][0] == "helper"
        assert "already imported" in result.skipped[0][1]

    def test_overwrite_flag(self, tmp_path):
        claude_dir = tmp_path / ".claude/agents"
        claude_dir.mkdir(parents=True)
        new_content = SAMPLE_MINIMAL_AGENT.replace("Generic helper", "UPDATED")
        (claude_dir / "helper.md").write_text(new_content)

        canonical = tmp_path / CANONICAL_AGENT_ROOT / "helper.md"
        canonical.parent.mkdir(parents=True)
        canonical.write_text("old")

        result = extract_agents_to_canonical(tmp_path)
        assert result.imported == []
        assert len(result.skipped) == 1
        assert "canonical exists" in result.skipped[0][1]
        assert canonical.read_text() == "old"

        result = extract_agents_to_canonical(tmp_path, overwrite=True)
        assert len(result.imported) == 1
        assert "UPDATED" in canonical.read_text()

    def test_ignores_codex_toml(self, tmp_path, codex_home):
        # Even when a Codex TOML exists, extract does not try to import it.
        (codex_home / ".codex/agents").mkdir(parents=True)
        (codex_home / ".codex/agents/helper.toml").write_text('name = "helper"\n')
        result = extract_agents_to_canonical(tmp_path)
        assert result.imported == []


class TestDiffAgents:
    def test_empty_project(self, tmp_path, codex_home):
        assert diff_agents(tmp_path) == []

    def test_missing_target_for_all(self, tmp_path, codex_home):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        rows = diff_agents(tmp_path)
        statuses = {status for _, _, status in rows}
        assert statuses == {"missing target"}

    def test_in_sync_after_generate(self, tmp_path, codex_home):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        generate_all_agents(tmp_path)
        rows = diff_agents(tmp_path)
        assert all(status == "in sync" for _, _, status in rows)

    def test_out_of_sync(self, tmp_path, codex_home):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        generate_all_agents(tmp_path)
        (tmp_path / ".claude/agents/helper.md").write_text("mutated")
        rows = diff_agents(tmp_path)
        status_by_runtime = {r: s for r, _, s in rows}
        assert status_by_runtime["claude_agents"] == "out of sync"
        assert status_by_runtime["gemini_agents"] == "in sync"
        assert status_by_runtime["codex_agents"] == "in sync"

    def test_missing_canonical(self, tmp_path, codex_home):
        claude_dir = tmp_path / ".claude/agents"
        claude_dir.mkdir(parents=True)
        (claude_dir / "runtime-only.md").write_text(SAMPLE_MINIMAL_AGENT)
        rows = diff_agents(tmp_path)
        assert any(status == "missing canonical" for _, _, status in rows)


class TestDetectAgentDirs:
    def test_empty(self, tmp_path):
        assert detect_agent_dirs(tmp_path) == []

    def test_detects_claude(self, tmp_path):
        d = tmp_path / ".claude/agents"
        d.mkdir(parents=True)
        (d / "reviewer.md").write_text(SAMPLE_FULL_AGENT)
        found = detect_agent_dirs(tmp_path)
        assert len(found) == 1
        assert found[0].agent == "claude_agents"
        assert found[0].kind == "agent_file"
        assert found[0].path.name == "reviewer.md"

    def test_detects_gemini(self, tmp_path):
        d = tmp_path / ".gemini/agents"
        d.mkdir(parents=True)
        (d / "tester.md").write_text(SAMPLE_MINIMAL_AGENT)
        found = detect_agent_dirs(tmp_path)
        assert found[0].agent == "gemini_agents"

    def test_codex_user_scope_not_in_project_detect(self, tmp_path, codex_home):
        (codex_home / ".codex/agents").mkdir(parents=True)
        (codex_home / ".codex/agents/helper.toml").write_text('name = "helper"\n')
        found = detect_agent_dirs(tmp_path)
        assert found == []


class TestOnDrop:
    def test_on_drop_warn_logs(self, tmp_path, caplog):
        _make_canonical_agent(tmp_path, "full", SAMPLE_FULL_AGENT)
        with caplog.at_level("WARNING"):
            result = generate_all_agents(
                tmp_path, runtimes=["claude_agents"], on_drop="warn"
            )
        # Claude drops kind + temperature — should still generate.
        assert len(result.generated) == 1
        assert result.dropped
        assert any("dropped" in r.message for r in caplog.records)

    def test_on_drop_error_raises(self, tmp_path):
        _make_canonical_agent(tmp_path, "full", SAMPLE_FULL_AGENT)
        with pytest.raises(StrictDropError):
            generate_all_agents(
                tmp_path, runtimes=["claude_agents"], on_drop="error"
            )

    def test_on_drop_ignore_is_silent(self, tmp_path, caplog):
        _make_canonical_agent(tmp_path, "full", SAMPLE_FULL_AGENT)
        with caplog.at_level("WARNING"):
            result = generate_all_agents(
                tmp_path, runtimes=["claude_agents"], on_drop="ignore"
            )
        assert len(result.generated) == 1
        assert result.dropped
        assert not any("dropped" in r.message for r in caplog.records)

    def test_strict_flag_still_works(self, tmp_path):
        """Legacy ``strict=True`` behaves like ``on_drop='error'``."""
        _make_canonical_agent(tmp_path, "full", SAMPLE_FULL_AGENT)
        with pytest.raises(StrictDropError):
            generate_all_agents(tmp_path, runtimes=["claude_agents"], strict=True)

    def test_on_drop_overrides_strict(self, tmp_path, caplog):
        """When both are supplied, ``on_drop`` wins."""
        _make_canonical_agent(tmp_path, "full", SAMPLE_FULL_AGENT)
        with caplog.at_level("WARNING"):
            result = generate_all_agents(
                tmp_path, runtimes=["claude_agents"], strict=True, on_drop="warn"
            )
        assert len(result.generated) == 1  # warn does not abort


class TestRoundtrip:
    def test_canonical_to_claude_and_back(self, tmp_path):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        generate_all_agents(tmp_path, runtimes=["claude_agents"])

        shutil.rmtree(tmp_path / CANONICAL_AGENT_ROOT)
        result = extract_agents_to_canonical(tmp_path)
        assert len(result.imported) == 1
        reparsed = parse_canonical_agent(
            tmp_path / CANONICAL_AGENT_ROOT / "helper.md"
        )
        assert reparsed.name == "helper"
        assert "Help with things." in reparsed.body


class TestDualScopeGenerators:
    """Tests for user-scope and project-scope generator variants."""

    def test_registry_has_all_generators(self):
        expected = {
            "claude_agents", "gemini_agents", "codex_agents",
            "claude_user_agents", "gemini_user_agents", "codex_project_agents",
        }
        assert expected == set(AGENT_GENERATORS.keys())

    def test_scope_fields(self):
        assert AGENT_GENERATORS["claude_agents"].scope == "project"
        assert AGENT_GENERATORS["codex_agents"].scope == "user"
        assert AGENT_GENERATORS["claude_user_agents"].scope == "user"
        assert AGENT_GENERATORS["codex_project_agents"].scope == "project"

    def test_default_fields(self):
        assert AGENT_GENERATORS["claude_agents"].default is True
        assert AGENT_GENERATORS["claude_user_agents"].default is False

    def test_scope_none_backward_compat(self, tmp_path, codex_home):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        result = generate_all_agents(tmp_path, scope=None)
        names = {r for r, _ in result.generated}
        assert names == {"claude_agents", "gemini_agents", "codex_agents"}

    def test_scope_project_only(self, tmp_path, codex_home):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        result = generate_all_agents(tmp_path, scope="project")
        names = {r for r, _ in result.generated}
        assert names == {"claude_agents", "gemini_agents", "codex_project_agents"}

    def test_scope_user_only(self, tmp_path, codex_home):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        result = generate_all_agents(tmp_path, scope="user")
        names = {r for r, _ in result.generated}
        assert names == {"codex_agents", "claude_user_agents", "gemini_user_agents"}

    def test_scope_all(self, tmp_path, codex_home):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        result = generate_all_agents(tmp_path, scope="all")
        assert len(result.generated) == 6

    def test_claude_user_writes_to_home(self, tmp_path, codex_home):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        generate_all_agents(tmp_path, runtimes=["claude_user_agents"])
        assert (codex_home / ".claude/agents/helper.md").is_file()
        assert not (tmp_path / ".claude/agents/helper.md").exists()

    def test_codex_project_writes_to_project(self, tmp_path, codex_home):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        generate_all_agents(tmp_path, runtimes=["codex_project_agents"])
        assert (tmp_path / ".codex/agents/helper.toml").is_file()

    def test_diff_scope_project(self, tmp_path, codex_home):
        _make_canonical_agent(tmp_path, "helper", SAMPLE_MINIMAL_AGENT)
        rows = diff_agents(tmp_path, scope="project")
        runtimes = {r for r, _, _ in rows}
        assert "codex_agents" not in runtimes
        assert "claude_agents" in runtimes
