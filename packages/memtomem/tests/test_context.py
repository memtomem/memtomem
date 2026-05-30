"""Tests for agent context management module."""

import pytest

from memtomem.context.parser import (
    iter_markdown_sections,
    parse_context,
    sections_to_markdown,
)
from memtomem.context.detector import detect_agent_files
from memtomem.context.generator import (
    GENERATORS,
    generate_for_agent,
    generate_all,
    extract_sections_from_agent_file,
)


SAMPLE_CONTEXT = """# Project Context

## Project
- Name: test-project
- Language: Python 3.12+

## Commands
- Build: pip install -e .
- Test: pytest
- Lint: ruff check .

## Architecture
Monorepo with src/ and tests/.

## Rules
- line-length 100
- pytest-asyncio auto mode

## Style
- English for code
- No emojis
"""


class TestParser:
    def test_parse_sections(self, tmp_path):
        ctx = tmp_path / "context.md"
        ctx.write_text(SAMPLE_CONTEXT, encoding="utf-8")
        sections = parse_context(ctx)

        assert "Project" in sections
        assert "Commands" in sections
        assert "Architecture" in sections
        assert "Rules" in sections
        assert "Style" in sections
        assert "test-project" in sections["Project"]

    def test_parse_nonexistent(self, tmp_path):
        result = parse_context(tmp_path / "nope.md")
        assert result == {}

    def test_roundtrip(self, tmp_path):
        ctx = tmp_path / "context.md"
        ctx.write_text(SAMPLE_CONTEXT, encoding="utf-8")
        sections = parse_context(ctx)
        output = sections_to_markdown(sections)
        reparsed = parse_context(tmp_path / "out.md")
        (tmp_path / "out.md").write_text(output, encoding="utf-8")
        reparsed = parse_context(tmp_path / "out.md")
        assert reparsed.keys() == sections.keys()


class TestDetector:
    def test_detect_claude(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# CLAUDE.md", encoding="utf-8")
        files = detect_agent_files(tmp_path)
        assert len(files) == 1
        assert files[0].agent == "claude"

    def test_detect_multiple(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# CLAUDE.md", encoding="utf-8")
        (tmp_path / ".cursorrules").write_text("rules", encoding="utf-8")
        (tmp_path / "GEMINI.md").write_text("# GEMINI.md", encoding="utf-8")
        files = detect_agent_files(tmp_path)
        agents = {f.agent for f in files}
        assert agents == {"claude", "cursor", "gemini"}

    def test_detect_empty(self, tmp_path):
        files = detect_agent_files(tmp_path)
        assert files == []

    def test_detect_codex(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# AGENTS.md", encoding="utf-8")
        files = detect_agent_files(tmp_path)
        assert files[0].agent == "codex"

    def test_detect_copilot(self, tmp_path):
        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "copilot-instructions.md").write_text("instructions", encoding="utf-8")
        files = detect_agent_files(tmp_path)
        assert files[0].agent == "copilot"


class TestGenerator:
    def _sections(self):
        return {
            "Project": "- Name: test\n- Language: Python",
            "Commands": "- Test: pytest",
            "Architecture": "Simple layout.",
            "Rules": "- line-length 100",
            "Style": "- English only",
        }

    def test_all_generators_registered(self):
        assert "claude" in GENERATORS
        assert "cursor" in GENERATORS
        assert "gemini" in GENERATORS
        assert "codex" in GENERATORS
        assert "copilot" in GENERATORS

    def test_claude_generate(self):
        content = generate_for_agent("claude", self._sections())
        assert "CLAUDE.md" in content
        assert "Claude Code" in content
        assert "pytest" in content

    def test_cursor_generate(self):
        content = generate_for_agent("cursor", self._sections())
        assert "line-length 100" in content
        assert "pytest" in content

    def test_gemini_generate(self):
        content = generate_for_agent("gemini", self._sections())
        assert "GEMINI.md" in content
        assert "Gemini CLI" in content

    def test_codex_generate(self):
        content = generate_for_agent("codex", self._sections())
        assert "AGENTS.md" in content

    def test_copilot_generate(self):
        content = generate_for_agent("copilot", self._sections())
        assert "line-length 100" in content

    def test_generate_all(self):
        result = generate_all(self._sections())
        assert len(result) == 5
        for name, content in result.items():
            assert len(content) > 0

    def test_unknown_agent_raises(self):
        with pytest.raises(KeyError):
            generate_for_agent("unknown", self._sections())


class TestExtractFromAgent:
    def test_extract_from_claude(self):
        content = """# CLAUDE.md

## What is this project?

A test project.

## Build & Development Commands

- Test: pytest

## Architecture

Simple.

## Coding Rules

- No magic numbers
"""
        sections = extract_sections_from_agent_file(content)
        assert "Project" in sections
        assert "Commands" in sections
        assert "Architecture" in sections
        assert "Rules" in sections

    def test_extract_preserves_unknown_headings(self):
        content = """## Custom Section

Some content here.
"""
        sections = extract_sections_from_agent_file(content)
        assert "Custom Section" in sections


class TestSpecificSectionRoundTrip:
    """`## <Agent>-Specific` sections must survive extract → generate.

    Regression: prior to alias addition, `extract_sections_from_agent_file`
    stored `## Claude-Specific` content under the literal key
    `Claude-Specific`, but the generator looks for the canonical key
    `Claude` — so the override section was silently dropped on round-trip.
    """

    @pytest.mark.parametrize(
        "agent,heading",
        [
            ("claude", "Claude-Specific"),
            ("cursor", "Cursor-Specific"),
            ("gemini", "Gemini-Specific"),
            ("codex", "Codex-Specific"),
            ("copilot", "Copilot-Specific"),
        ],
    )
    def test_specific_section_roundtrip(self, agent, heading):
        marker = "this content must survive round-trip"
        original = f"""# AGENT_FILE

## What is this project?

- Name: foo

## {heading}

{marker}
"""
        sections = extract_sections_from_agent_file(original)
        # Canonical key is the agent name with capital first letter.
        canonical = heading.split("-", 1)[0]
        assert canonical in sections
        assert marker in sections[canonical]

        regenerated = generate_for_agent(agent, sections)
        assert heading in regenerated
        assert marker in regenerated


class TestParserHardening:
    """Round-trip data-loss guards for the section parser (#1123 B1)."""

    def test_fenced_code_hashes_not_treated_as_headings(self, tmp_path):
        """`##` inside a fenced code block must not split the section (B1-1)."""
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "# Project Context\n\n"
            "## Architecture\n\n"
            "Run the example:\n\n"
            "```python\n"
            "## This looks like a heading but it is code\n"
            "value = 1\n"
            "```\n\n"
            "Trailing prose under Architecture.\n\n"
            "## Rules\n\n"
            "- line-length 100\n",
            encoding="utf-8",
        )
        sections = parse_context(ctx)

        # No spurious section minted from the in-code `##` line.
        assert set(sections) == {"Architecture", "Rules"}
        # Both the code block and the prose after it stay under Architecture.
        assert "## This looks like a heading but it is code" in sections["Architecture"]
        assert "Trailing prose under Architecture." in sections["Architecture"]
        assert sections["Rules"] == "- line-length 100"

    def test_tilde_fence_also_guarded(self):
        """`~~~` fences are guarded the same as backtick fences (B1-1)."""
        text = "## Architecture\n~~~\n## not a heading\n~~~\nafter\n## Rules\n- r\n"
        names = [name for name, _ in iter_markdown_sections(text)]
        assert names == ["Architecture", "Rules"]

    def test_duplicate_headings_merge_not_overwrite(self, tmp_path):
        """A repeated `## Name` must concatenate, not drop the first (B1-2)."""
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "## Project\nfirst body line\n## Project\nsecond body line\n",
            encoding="utf-8",
        )
        sections = parse_context(ctx)

        assert list(sections) == ["Project"]
        assert "first body line" in sections["Project"]
        assert "second body line" in sections["Project"]

    def test_aliased_duplicate_headings_merge_on_extract(self):
        """Two headings aliasing to one canonical key must merge (B1-2)."""
        content = "## Rules\n- from rules heading\n## Coding Rules\n- from coding-rules heading\n"
        sections = extract_sections_from_agent_file(content)

        assert "from rules heading" in sections["Rules"]
        assert "from coding-rules heading" in sections["Rules"]

    def test_whitespace_only_heading_not_a_section(self, tmp_path):
        """`##` with no name must not create an empty-string key (B1-3)."""
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "## Project\nreal content\n##   \norphan content\n",
            encoding="utf-8",
        )
        sections = parse_context(ctx)

        assert "" not in sections
        assert list(sections) == ["Project"]
        # The malformed heading line and its body fold into the open section.
        assert "orphan content" in sections["Project"]

    def test_sample_context_still_round_trips(self, tmp_path):
        """The happy-path round-trip is unchanged by the hardening."""
        ctx = tmp_path / "context.md"
        ctx.write_text(SAMPLE_CONTEXT, encoding="utf-8")
        sections = parse_context(ctx)
        out = tmp_path / "out.md"
        out.write_text(sections_to_markdown(sections), encoding="utf-8")
        reparsed = parse_context(out)
        assert reparsed == sections
