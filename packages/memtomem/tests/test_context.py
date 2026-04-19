"""Tests for agent context management module."""

import pytest

from memtomem.context.parser import parse_context, sections_to_markdown
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
