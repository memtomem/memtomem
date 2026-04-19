"""Tests for context/agents.py — canonical ⇄ runtime sub-agent fan-out."""

import shutil
import tomllib

import pytest

from memtomem.context.agents import (
    AGENT_GENERATORS,
    CANONICAL_AGENT_ROOT,
    AgentParseError,
    StrictDropError,
    _toml_escape_basic_string,
    _toml_escape_multiline_string,
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

    @pytest.mark.parametrize(
        "hostile_name",
        [
            "../../evil",
            "../escape",
            "a/b",
            "a\\b",
            ".",
            "..",
            "-x",
            "A" * 65,
            "name with space",
        ],
    )
    def test_rejects_hostile_name_in_frontmatter(self, tmp_path, hostile_name):
        """#276: ``name:`` frontmatter must not land us outside the target dir."""
        p = tmp_path / "hostile.md"
        p.write_text(f"---\nname: {hostile_name}\ndescription: x\n---\n\nbody\n")
        with pytest.raises(AgentParseError, match="invalid agent name"):
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
        _make_canonical_agent(tmp_path, "zeta", SAMPLE_MINIMAL_AGENT.replace("helper", "zeta"))
        _make_canonical_agent(tmp_path, "alpha", SAMPLE_MINIMAL_AGENT.replace("helper", "alpha"))
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

    def test_skips_hostile_runtime_filename(self, tmp_path):
        """#276: a runtime directory containing ``-x.md`` (leading dash) round-trips
        into a canonical filename that validate_name rejects, so we skip rather
        than produce a canonical file we'd refuse to read back."""
        claude_dir = tmp_path / ".claude/agents"
        claude_dir.mkdir(parents=True)
        # File name with leading dash — unusual but filesystem-legal.
        (claude_dir / "-bad.md").write_text(SAMPLE_MINIMAL_AGENT)
        (claude_dir / "ok.md").write_text(SAMPLE_MINIMAL_AGENT)

        result = extract_agents_to_canonical(tmp_path)

        # Only "ok" imported; "-bad" skipped with invalid-name reason.
        imported_names = sorted(p.stem for p in result.imported)
        assert imported_names == ["ok"]
        skipped_names = sorted(name for name, _ in result.skipped)
        assert "-bad" in skipped_names
        reason = dict(result.skipped)["-bad"]
        assert "invalid name" in reason


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
            result = generate_all_agents(tmp_path, runtimes=["claude_agents"], on_drop="warn")
        # Claude drops kind + temperature — should still generate.
        assert len(result.generated) == 1
        assert result.dropped
        assert any("dropped" in r.message for r in caplog.records)

    def test_on_drop_error_raises(self, tmp_path):
        _make_canonical_agent(tmp_path, "full", SAMPLE_FULL_AGENT)
        with pytest.raises(StrictDropError):
            generate_all_agents(tmp_path, runtimes=["claude_agents"], on_drop="error")

    def test_on_drop_ignore_is_silent(self, tmp_path, caplog):
        _make_canonical_agent(tmp_path, "full", SAMPLE_FULL_AGENT)
        with caplog.at_level("WARNING"):
            result = generate_all_agents(tmp_path, runtimes=["claude_agents"], on_drop="ignore")
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
        reparsed = parse_canonical_agent(tmp_path / CANONICAL_AGENT_ROOT / "helper.md")
        assert reparsed.name == "helper"
        assert "Help with things." in reparsed.body


class TestCrlfParsing:
    """#279: frontmatter regex anchors on ``\\n`` — CRLF files must still parse."""

    def test_crlf_frontmatter_parses(self, tmp_path):
        p = tmp_path / "crlf.md"
        p.write_bytes(SAMPLE_FULL_AGENT.replace("\n", "\r\n").encode("utf-8"))
        agent = parse_canonical_agent(p)
        assert agent.name == "code-reviewer"
        assert agent.tools == ["Read", "Grep", "Glob"]
        assert agent.model == "sonnet"
        # Body is LF-normalized after parse; no raw \r should leak through.
        assert "\r" not in agent.body

    def test_crlf_roundtrip_is_idempotent(self, tmp_path):
        """Canonical (CRLF on disk) → runtime → canonical must be byte-stable."""
        p = tmp_path / CANONICAL_AGENT_ROOT / "helper.md"
        p.parent.mkdir(parents=True)
        p.write_bytes(SAMPLE_MINIMAL_AGENT.replace("\n", "\r\n").encode("utf-8"))

        generate_all_agents(tmp_path, runtimes=["claude_agents"])
        rendered_first = (tmp_path / ".claude/agents/helper.md").read_bytes()

        # Re-parse the canonical (still CRLF on disk) and regenerate.
        generate_all_agents(tmp_path, runtimes=["claude_agents"])
        rendered_second = (tmp_path / ".claude/agents/helper.md").read_bytes()

        assert rendered_first == rendered_second
        # And the rendered runtime file uses LF, not CRLF.
        assert b"\r\n" not in rendered_first


class TestUnknownFrontmatterKeys:
    def test_unknown_key_warns_with_path(self, tmp_path, caplog):
        body = (
            "---\n"
            "name: odd\n"
            "description: has a future key\n"
            "future_field: value\n"
            "another_unknown: [a, b]\n"
            "---\n\n"
            "body\n"
        )
        p = tmp_path / "odd.md"
        p.write_text(body)
        with caplog.at_level("WARNING"):
            agent = parse_canonical_agent(p)
        # Known fields still parse; unknowns are dropped (not stored on SubAgent).
        assert agent.name == "odd"
        messages = [r.getMessage() for r in caplog.records]
        assert any("unknown frontmatter keys" in m for m in messages)
        assert any(str(p) in m for m in messages)
        # Alphabetized for stable output.
        assert any("another_unknown" in m and "future_field" in m for m in messages)

    def test_known_keys_do_not_warn(self, tmp_path, caplog):
        _make_canonical_agent(tmp_path, "full", SAMPLE_FULL_AGENT)
        with caplog.at_level("WARNING"):
            parse_canonical_agent(tmp_path / CANONICAL_AGENT_ROOT / "full.md")
        assert not any("unknown frontmatter" in r.getMessage() for r in caplog.records)


class TestCodexTomlControlChars:
    """#279: Codex TOML must parse back under ``tomllib.loads`` even when the
    canonical body contains raw C0 control characters."""

    def test_body_with_c0_controls_parses(self, tmp_path, codex_home):
        """Bodies routed into a multiline TOML string must escape C0 controls
        that the TOML spec still requires escaping even inside triple quotes
        (sub-0x20 chars aside from ``\\n``/``\\t``). ``\\r`` is pre-normalized
        by ``Path.read_text`` (universal newlines), so it can't reach here on
        the read path — it's exercised directly in :class:`TestTomlEscape`."""
        body = (
            "---\n"
            "name: odd-body\n"
            "description: has control chars\n"
            "---\n\n"
            "line with NUL: \x00 and ESC: \x1b and BS: \x08\n"
        )
        p = tmp_path / CANONICAL_AGENT_ROOT / "odd-body.md"
        p.parent.mkdir(parents=True)
        p.write_text(body)

        generate_all_agents(tmp_path, runtimes=["codex_agents"])
        toml_path = codex_home / ".codex/agents/odd-body.toml"
        parsed = tomllib.loads(toml_path.read_text())
        # Control chars survive the round-trip through tomllib.
        assert "\x00" in parsed["developer_instructions"]
        assert "\x1b" in parsed["developer_instructions"]
        assert "\x08" in parsed["developer_instructions"]

    def test_multiline_body_with_triple_quote_is_escaped(self, tmp_path, codex_home):
        body = (
            "---\n"
            "name: triple\n"
            "description: body contains triple quote\n"
            '---\n\nHere is a stray triple: """ inside text\nSecond line.\n'
        )
        p = tmp_path / CANONICAL_AGENT_ROOT / "triple.md"
        p.parent.mkdir(parents=True)
        p.write_text(body)

        generate_all_agents(tmp_path, runtimes=["codex_agents"])
        toml_path = codex_home / ".codex/agents/triple.toml"
        parsed = tomllib.loads(toml_path.read_text())
        assert '"""' in parsed["developer_instructions"]


class TestTomlEscape:
    """Direct unit tests for the TOML escape helpers — documents which chars
    get which escape form so future edits don't silently narrow the set."""

    def test_basic_string_named_escapes(self):
        s = 'back\\slash, quote", bs\b, tab\t, lf\n, ff\f, cr\r'
        escaped = _toml_escape_basic_string(s)
        assert "\\\\" in escaped
        assert '\\"' in escaped
        assert "\\b" in escaped
        assert "\\t" in escaped
        assert "\\n" in escaped
        assert "\\f" in escaped
        assert "\\r" in escaped
        # Round-trips through tomllib.
        parsed = tomllib.loads(f'v = "{escaped}"')
        assert parsed["v"] == s

    def test_basic_string_other_c0_uses_uxxxx(self):
        s = "nul\x00, soh\x01, esc\x1b, del\x7f"
        escaped = _toml_escape_basic_string(s)
        assert "\\u0000" in escaped
        assert "\\u0001" in escaped
        assert "\\u001b" in escaped
        assert "\\u007f" in escaped
        parsed = tomllib.loads(f'v = "{escaped}"')
        assert parsed["v"] == s

    def test_multiline_keeps_newline_and_tab_literal(self):
        s = "line1\nline2\twith tab"
        escaped = _toml_escape_multiline_string(s)
        # Newline/tab stay literal in triple-quoted form.
        assert "\n" in escaped
        assert "\t" in escaped
        assert "\\n" not in escaped
        assert "\\t" not in escaped

    def test_multiline_still_escapes_cr_and_c0(self):
        s = "cr\r middle nul\x00 end\x1b"
        escaped = _toml_escape_multiline_string(s)
        assert "\\r" in escaped
        assert "\\u0000" in escaped
        assert "\\u001b" in escaped
        parsed = tomllib.loads(f'v = """\n{escaped}"""')
        assert parsed["v"] == s

    def test_multiline_breaks_triple_quote(self):
        s = 'prefix """ suffix'
        escaped = _toml_escape_multiline_string(s)
        # Raw ``"""`` would close the string — must be broken.
        assert '"""' not in escaped
        parsed = tomllib.loads(f'v = """\n{escaped}"""')
        assert '"""' in parsed["v"]
