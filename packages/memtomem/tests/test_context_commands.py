"""Tests for context/commands.py — canonical ⇄ runtime slash command fan-out."""

import shutil
import tomllib

import pytest

from memtomem.context.commands import (
    CANONICAL_COMMAND_ROOT,
    COMMAND_GENERATORS,
    CommandSyncResult,
    StrictDropError,
    diff_commands,
    extract_commands_to_canonical,
    generate_all_commands,
    list_canonical_commands,
    parse_canonical_command,
)
from memtomem.context.detector import detect_command_dirs


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    """Redirect HOME so Codex prompt writes don't touch the real ``~/.codex/prompts/``."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))  # Windows safety (no-op on macOS)
    return fake_home


SAMPLE_FULL_COMMAND = """---
description: Review a file for issues
argument-hint: [file-path]
allowed-tools: [Read, Grep]
model: sonnet
---

Review the file at $ARGUMENTS for issues.
Report a prioritized punch list.
"""

SAMPLE_MINIMAL_COMMAND = """---
description: Simple prompt
---

Say hi to $ARGUMENTS.
"""


def _make_canonical_command(project_root, name, body=SAMPLE_FULL_COMMAND):
    root = project_root / CANONICAL_COMMAND_ROOT
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.md"
    path.write_text(body)
    return path


class TestParseCanonicalCommand:
    def test_parses_all_fields(self, tmp_path):
        p = _make_canonical_command(tmp_path, "review", SAMPLE_FULL_COMMAND)
        cmd = parse_canonical_command(p)
        assert cmd.name == "review"
        assert cmd.description == "Review a file for issues"
        assert cmd.argument_hint == "[file-path]"
        assert cmd.allowed_tools == ["Read", "Grep"]
        assert cmd.model == "sonnet"
        assert "Review the file at $ARGUMENTS" in cmd.body

    def test_parses_minimal(self, tmp_path):
        p = _make_canonical_command(tmp_path, "hi", SAMPLE_MINIMAL_COMMAND)
        cmd = parse_canonical_command(p)
        assert cmd.name == "hi"
        assert cmd.description == "Simple prompt"
        assert cmd.argument_hint is None
        assert cmd.allowed_tools == []
        assert cmd.model is None
        assert "Say hi to $ARGUMENTS" in cmd.body

    def test_tolerates_missing_frontmatter(self, tmp_path):
        p = tmp_path / "bare.md"
        p.write_text("Just a bare prompt with $ARGUMENTS.")
        cmd = parse_canonical_command(p)
        assert cmd.name == "bare"
        assert cmd.description == ""
        assert "Just a bare prompt" in cmd.body


class TestListCanonicalCommands:
    def test_empty(self, tmp_path):
        assert list_canonical_commands(tmp_path) == []

    def test_sorted(self, tmp_path):
        _make_canonical_command(tmp_path, "zeta", SAMPLE_MINIMAL_COMMAND)
        _make_canonical_command(tmp_path, "alpha", SAMPLE_MINIMAL_COMMAND)
        names = [p.stem for p in list_canonical_commands(tmp_path)]
        assert names == ["alpha", "zeta"]


class TestClaudeCommandRendering:
    def test_passes_through_all_fields(self, tmp_path):
        _make_canonical_command(tmp_path, "review", SAMPLE_FULL_COMMAND)
        generate_all_commands(tmp_path, runtimes=["claude_commands"])
        out = (tmp_path / ".claude/commands/review.md").read_text()
        assert "description: Review a file for issues" in out
        assert "argument-hint: [file-path]" in out
        assert "allowed-tools: [Read, Grep]" in out
        assert "model: sonnet" in out
        assert "$ARGUMENTS" in out  # placeholder preserved

    def test_no_dropped_fields(self, tmp_path):
        _make_canonical_command(tmp_path, "review", SAMPLE_FULL_COMMAND)
        result = generate_all_commands(tmp_path, runtimes=["claude_commands"])
        assert result.dropped == []

    def test_frontmatter_omitted_when_all_fields_empty(self, tmp_path):
        body = "Just the prompt with $ARGUMENTS.\n"
        p = tmp_path / CANONICAL_COMMAND_ROOT / "bare.md"
        p.parent.mkdir(parents=True)
        p.write_text(body)  # no frontmatter at all
        generate_all_commands(tmp_path, runtimes=["claude_commands"])
        out = (tmp_path / ".claude/commands/bare.md").read_text()
        assert out.startswith("Just the prompt")
        assert "---" not in out


class TestGeminiCommandRendering:
    def test_writes_valid_toml(self, tmp_path):
        _make_canonical_command(tmp_path, "review", SAMPLE_FULL_COMMAND)
        generate_all_commands(tmp_path, runtimes=["gemini_commands"])
        toml_path = tmp_path / ".gemini/commands/review.toml"
        assert toml_path.is_file()
        parsed = tomllib.loads(toml_path.read_text())
        assert parsed["description"] == "Review a file for issues"
        assert "prompt" in parsed
        assert "Review the file at {{args}}" in parsed["prompt"]

    def test_rewrites_arguments_placeholder(self, tmp_path):
        _make_canonical_command(tmp_path, "review", SAMPLE_FULL_COMMAND)
        generate_all_commands(tmp_path, runtimes=["gemini_commands"])
        parsed = tomllib.loads((tmp_path / ".gemini/commands/review.toml").read_text())
        assert "$ARGUMENTS" not in parsed["prompt"]
        assert "{{args}}" in parsed["prompt"]

    def test_drops_claude_only_fields(self, tmp_path):
        _make_canonical_command(tmp_path, "review", SAMPLE_FULL_COMMAND)
        result = generate_all_commands(tmp_path, runtimes=["gemini_commands"])
        fields = result.dropped[0][2]
        assert "argument-hint" in fields
        assert "allowed-tools" in fields
        assert "model" in fields

    def test_minimal_command_has_no_dropped_fields(self, tmp_path):
        _make_canonical_command(tmp_path, "hi", SAMPLE_MINIMAL_COMMAND)
        result = generate_all_commands(tmp_path, runtimes=["gemini_commands"])
        assert result.dropped == []


class TestCodexCommandRendering:
    def test_renders_minimal_command(self, tmp_path, codex_home):
        _make_canonical_command(tmp_path, "hi", SAMPLE_MINIMAL_COMMAND)
        generate_all_commands(tmp_path, runtimes=["codex_commands"])
        out_path = codex_home / ".codex/prompts/hi.md"
        assert out_path.is_file()
        out = out_path.read_text()
        assert "description: Simple prompt" in out
        assert "Say hi to $ARGUMENTS" in out

    def test_renders_full_command(self, tmp_path, codex_home):
        _make_canonical_command(tmp_path, "review", SAMPLE_FULL_COMMAND)
        generate_all_commands(tmp_path, runtimes=["codex_commands"])
        out = (codex_home / ".codex/prompts/review.md").read_text()
        assert "description: Review a file for issues" in out
        assert "argument-hint: [file-path]" in out
        # allowed-tools and model are dropped — Codex has no equivalents.
        assert "allowed-tools" not in out
        assert "model:" not in out
        assert "Review the file at $ARGUMENTS" in out

    def test_preserves_arguments_placeholder(self, tmp_path, codex_home):
        """REGRESSION GUARD: Codex's $ARGUMENTS is native — do NOT rewrite it.
        A prior Phase 3.5 attempt broke semantics by rewriting $ARGUMENTS → $1."""
        _make_canonical_command(tmp_path, "review", SAMPLE_FULL_COMMAND)
        generate_all_commands(tmp_path, runtimes=["codex_commands"])
        out = (codex_home / ".codex/prompts/review.md").read_text()
        assert "$ARGUMENTS" in out
        assert "$1" not in out
        assert "{{args}}" not in out

    def test_preserves_positional_and_named_placeholders(self, tmp_path, codex_home):
        body = (
            "---\ndescription: Mixed placeholders\n---\n\n"
            "First: $1, second: $2, named: $PRIORITY, whole: $ARGUMENTS, dollar: $$\n"
        )
        _make_canonical_command(tmp_path, "mixed", body)
        generate_all_commands(tmp_path, runtimes=["codex_commands"])
        out = (codex_home / ".codex/prompts/mixed.md").read_text()
        assert "$1" in out
        assert "$2" in out
        assert "$PRIORITY" in out
        assert "$ARGUMENTS" in out
        assert "$$" in out

    def test_drops_allowed_tools_and_model(self, tmp_path, codex_home):
        _make_canonical_command(tmp_path, "review", SAMPLE_FULL_COMMAND)
        result = generate_all_commands(tmp_path, runtimes=["codex_commands"])
        fields = result.dropped[0][2]
        assert "allowed-tools" in fields
        assert "model" in fields
        # argument-hint is NOT dropped — Codex supports it natively.
        assert "argument-hint" not in fields

    def test_minimal_command_has_no_dropped_fields(self, tmp_path, codex_home):
        _make_canonical_command(tmp_path, "hi", SAMPLE_MINIMAL_COMMAND)
        result = generate_all_commands(tmp_path, runtimes=["codex_commands"])
        assert result.dropped == []

    def test_user_scope_path_uses_home(self, tmp_path, codex_home):
        _make_canonical_command(tmp_path, "hi", SAMPLE_MINIMAL_COMMAND)
        generate_all_commands(tmp_path, runtimes=["codex_commands"])
        # project_root is intentionally ignored — the file lives under fake HOME,
        # not under tmp_path.
        assert (codex_home / ".codex/prompts/hi.md").is_file()
        assert not (tmp_path / ".codex/prompts/hi.md").exists()

    def test_frontmatter_omitted_when_no_fields(self, tmp_path, codex_home):
        body = "Just a bare prompt with $ARGUMENTS.\n"
        p = tmp_path / CANONICAL_COMMAND_ROOT / "bare.md"
        p.parent.mkdir(parents=True)
        p.write_text(body)  # no frontmatter at all
        generate_all_commands(tmp_path, runtimes=["codex_commands"])
        out = (codex_home / ".codex/prompts/bare.md").read_text()
        assert out.startswith("Just a bare prompt")
        assert "---" not in out


class TestGenerateAllCommands:
    def test_fans_out_to_all_three_runtimes(self, tmp_path, codex_home):
        _make_canonical_command(tmp_path, "hi", SAMPLE_MINIMAL_COMMAND)
        result = generate_all_commands(tmp_path)
        assert isinstance(result, CommandSyncResult)
        assert len(result.generated) == 3
        assert (tmp_path / ".claude/commands/hi.md").is_file()
        assert (tmp_path / ".gemini/commands/hi.toml").is_file()
        assert (codex_home / ".codex/prompts/hi.md").is_file()

    def test_no_canonical_no_op(self, tmp_path):
        result = generate_all_commands(tmp_path)
        assert result.generated == []
        assert result.skipped == [("<all>", "no canonical commands")]

    def test_registry_contents(self):
        assert "claude_commands" in COMMAND_GENERATORS
        assert "gemini_commands" in COMMAND_GENERATORS
        assert "codex_commands" in COMMAND_GENERATORS

    def test_unknown_runtime_skipped(self, tmp_path):
        _make_canonical_command(tmp_path, "hi", SAMPLE_MINIMAL_COMMAND)
        result = generate_all_commands(tmp_path, runtimes=["claude_commands", "nope"])
        assert ("nope", "unknown runtime") in result.skipped


class TestStrictMode:
    def test_strict_raises_on_dropped_fields(self, tmp_path):
        _make_canonical_command(tmp_path, "review", SAMPLE_FULL_COMMAND)
        with pytest.raises(StrictDropError):
            generate_all_commands(tmp_path, runtimes=["gemini_commands"], strict=True)

    def test_strict_passes_with_minimal(self, tmp_path, codex_home):
        _make_canonical_command(tmp_path, "hi", SAMPLE_MINIMAL_COMMAND)
        result = generate_all_commands(tmp_path, strict=True)
        assert len(result.generated) == 3


class TestExtractCommandsToCanonical:
    def test_imports_claude_command(self, tmp_path):
        d = tmp_path / ".claude/commands"
        d.mkdir(parents=True)
        (d / "review.md").write_text(SAMPLE_FULL_COMMAND)
        imported = extract_commands_to_canonical(tmp_path)
        assert len(imported) == 1
        assert (tmp_path / CANONICAL_COMMAND_ROOT / "review.md").is_file()

    def test_imports_gemini_toml_with_placeholder_rewrite(self, tmp_path):
        d = tmp_path / ".gemini/commands"
        d.mkdir(parents=True)
        (d / "review.toml").write_text(
            'description = "Review a file"\nprompt = "Review {{args}} and report issues."\n'
        )
        imported = extract_commands_to_canonical(tmp_path)
        assert len(imported) == 1
        canonical = (tmp_path / CANONICAL_COMMAND_ROOT / "review.md").read_text()
        assert "description: Review a file" in canonical
        # {{args}} rewritten back to $ARGUMENTS
        assert "$ARGUMENTS" in canonical
        assert "{{args}}" not in canonical

    def test_claude_wins_over_gemini(self, tmp_path):
        for runtime, filename, content in (
            (".claude/commands", "shared.md", SAMPLE_MINIMAL_COMMAND),
            (".gemini/commands", "shared.toml", 'prompt = "gemini"\n'),
        ):
            d = tmp_path / runtime
            d.mkdir(parents=True)
            (d / filename).write_text(content)
        imported = extract_commands_to_canonical(tmp_path)
        assert len(imported) == 1
        canonical = (tmp_path / CANONICAL_COMMAND_ROOT / "shared.md").read_text()
        assert "Simple prompt" in canonical  # claude version won

    def test_overwrite_flag(self, tmp_path):
        d = tmp_path / ".claude/commands"
        d.mkdir(parents=True)
        new = SAMPLE_MINIMAL_COMMAND.replace("Simple prompt", "UPDATED")
        (d / "hi.md").write_text(new)

        canonical = tmp_path / CANONICAL_COMMAND_ROOT / "hi.md"
        canonical.parent.mkdir(parents=True)
        canonical.write_text("old")

        imported = extract_commands_to_canonical(tmp_path)
        assert imported == []
        assert canonical.read_text() == "old"

        imported = extract_commands_to_canonical(tmp_path, overwrite=True)
        assert len(imported) == 1
        assert "UPDATED" in canonical.read_text()

    def test_ignores_codex_prompts(self, tmp_path, codex_home):
        """Codex ~/.codex/prompts/ is user-scope — extract intentionally skips it
        even though the format is byte-compatible with Claude. Parity with the
        Phase 2 ``test_ignores_codex_toml`` policy."""
        (codex_home / ".codex/prompts").mkdir(parents=True)
        (codex_home / ".codex/prompts/runtime-only.md").write_text(SAMPLE_MINIMAL_COMMAND)
        imported = extract_commands_to_canonical(tmp_path)
        assert imported == []


class TestDiffCommands:
    def test_empty_project(self, tmp_path, codex_home):
        assert diff_commands(tmp_path) == []

    def test_missing_target(self, tmp_path, codex_home):
        _make_canonical_command(tmp_path, "hi", SAMPLE_MINIMAL_COMMAND)
        rows = diff_commands(tmp_path)
        assert rows
        assert all(status == "missing target" for _, _, status in rows)

    def test_in_sync_after_generate(self, tmp_path, codex_home):
        _make_canonical_command(tmp_path, "hi", SAMPLE_MINIMAL_COMMAND)
        generate_all_commands(tmp_path)
        rows = diff_commands(tmp_path)
        assert all(status == "in sync" for _, _, status in rows)

    def test_out_of_sync(self, tmp_path, codex_home):
        _make_canonical_command(tmp_path, "hi", SAMPLE_MINIMAL_COMMAND)
        generate_all_commands(tmp_path)
        (tmp_path / ".claude/commands/hi.md").write_text("mutated")
        rows = diff_commands(tmp_path)
        status_by_runtime = {r: s for r, _, s in rows}
        assert status_by_runtime["claude_commands"] == "out of sync"
        assert status_by_runtime["gemini_commands"] == "in sync"
        assert status_by_runtime["codex_commands"] == "in sync"

    def test_missing_canonical(self, tmp_path, codex_home):
        d = tmp_path / ".claude/commands"
        d.mkdir(parents=True)
        (d / "runtime-only.md").write_text(SAMPLE_MINIMAL_COMMAND)
        rows = diff_commands(tmp_path)
        assert any(status == "missing canonical" for _, _, status in rows)

    def test_codex_user_scope_missing_canonical(self, tmp_path, codex_home):
        """When a Codex prompt exists user-scope but no canonical file backs it,
        ``diff_commands`` reports ``missing canonical`` for the ``codex_commands``
        runtime — verifies the ``_runtime_command_names`` dispatch picks up
        ``Path.home() / .codex/prompts``."""
        (codex_home / ".codex/prompts").mkdir(parents=True)
        (codex_home / ".codex/prompts/runtime-only.md").write_text(SAMPLE_MINIMAL_COMMAND)
        rows = diff_commands(tmp_path)
        codex_rows = [(n, s) for r, n, s in rows if r == "codex_commands"]
        assert ("runtime-only", "missing canonical") in codex_rows


class TestDetectCommandDirs:
    def test_empty(self, tmp_path):
        assert detect_command_dirs(tmp_path) == []

    def test_detects_claude(self, tmp_path):
        d = tmp_path / ".claude/commands"
        d.mkdir(parents=True)
        (d / "review.md").write_text(SAMPLE_MINIMAL_COMMAND)
        found = detect_command_dirs(tmp_path)
        assert len(found) == 1
        assert found[0].agent == "claude_commands"
        assert found[0].kind == "command_file"

    def test_detects_gemini_toml(self, tmp_path):
        d = tmp_path / ".gemini/commands"
        d.mkdir(parents=True)
        (d / "review.toml").write_text('prompt = "hi"\n')
        found = detect_command_dirs(tmp_path)
        assert len(found) == 1
        assert found[0].agent == "gemini_commands"
        assert found[0].path.suffix == ".toml"

    def test_ignores_wrong_extension(self, tmp_path):
        # .md inside .gemini/commands is NOT a Gemini command — skip it.
        d = tmp_path / ".gemini/commands"
        d.mkdir(parents=True)
        (d / "stray.md").write_text("not a toml command")
        found = detect_command_dirs(tmp_path)
        assert found == []

    def test_codex_user_scope_not_in_project_detect(self, tmp_path, codex_home):
        """detect_command_dirs only scans project-scope — Codex user-scope
        prompts must never surface. Symmetric with the Phase 2 agents test of
        the same name."""
        (codex_home / ".codex/prompts").mkdir(parents=True)
        (codex_home / ".codex/prompts/helper.md").write_text(SAMPLE_MINIMAL_COMMAND)
        found = detect_command_dirs(tmp_path)
        assert found == []


class TestRoundtrip:
    def test_canonical_to_claude_and_back(self, tmp_path):
        _make_canonical_command(tmp_path, "review", SAMPLE_FULL_COMMAND)
        generate_all_commands(tmp_path, runtimes=["claude_commands"])

        shutil.rmtree(tmp_path / CANONICAL_COMMAND_ROOT)
        imported = extract_commands_to_canonical(tmp_path)
        assert len(imported) == 1
        reparsed = parse_canonical_command(tmp_path / CANONICAL_COMMAND_ROOT / "review.md")
        assert reparsed.name == "review"
        assert "$ARGUMENTS" in reparsed.body

    def test_canonical_to_gemini_and_back(self, tmp_path):
        # Minimal command so no fields get dropped on the Gemini side.
        _make_canonical_command(tmp_path, "hi", SAMPLE_MINIMAL_COMMAND)
        generate_all_commands(tmp_path, runtimes=["gemini_commands"])

        shutil.rmtree(tmp_path / CANONICAL_COMMAND_ROOT)
        imported = extract_commands_to_canonical(tmp_path)
        assert len(imported) == 1
        canonical = (tmp_path / CANONICAL_COMMAND_ROOT / "hi.md").read_text()
        assert "description: Simple prompt" in canonical
        assert "$ARGUMENTS" in canonical
        assert "{{args}}" not in canonical
