"""Regression: `mm context sync` / mem_context_sync must NOT overwrite existing
agent files when context.md parses to zero sections.

``parse_context`` returns ``{}`` for any context.md that has content but no
``## Heading`` delimiters (a stub, an all-prose file, or a file the user is
mid-edit on). Before the guard, ``gen.generate({})`` produced header-only /
empty output that ``sync`` wrote straight over the user's existing
CLAUDE.md / GEMINI.md / .cursorrules — silent data loss with no backup. The
sibling ``generate`` verb has always had this guard; ``sync`` (and its MCP
twin) did not. These tests pin the parity.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.context.parser import CONTEXT_FILENAME
from memtomem.server.tools.context import mem_context_sync

from .helpers import set_home

PROSE_ONLY = "Just some working notes with no markdown headings yet.\nMore prose.\n"
WITH_SECTION = "# Context\n\n## Project\n- Name: demo\n- Language: Python\n"
ORIGINAL_CLAUDE = "# My real project rules\n\nIMPORTANT: do not delete this.\n"


def _seed_project(tmp_path: Path, context_body: str) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    ctx = project / CONTEXT_FILENAME
    ctx.parent.mkdir(parents=True, exist_ok=True)
    ctx.write_text(context_body, encoding="utf-8")
    (project / "CLAUDE.md").write_text(ORIGINAL_CLAUDE, encoding="utf-8")
    return project


class TestCliSyncEmptyGuard:
    def test_sync_refuses_to_overwrite_when_no_sections(self, tmp_path, monkeypatch):
        project = _seed_project(tmp_path, PROSE_ONLY)
        monkeypatch.chdir(project)

        from memtomem.cli.context_cmd import context

        result = CliRunner().invoke(context, ["sync"])

        assert result.exit_code == 0
        assert "empty" in result.output.lower()
        # The user's existing CLAUDE.md must be untouched.
        assert (project / "CLAUDE.md").read_text(encoding="utf-8") == ORIGINAL_CLAUDE

    def test_sync_still_writes_when_sections_present(self, tmp_path, monkeypatch):
        """The guard must NOT block the happy path."""
        project = _seed_project(tmp_path, WITH_SECTION)
        monkeypatch.chdir(project)

        from memtomem.cli.context_cmd import context

        result = CliRunner().invoke(context, ["sync"])

        assert result.exit_code == 0
        # CLAUDE.md was regenerated from the parsed sections, replacing the sentinel.
        assert (project / "CLAUDE.md").read_text(encoding="utf-8") != ORIGINAL_CLAUDE


@pytest.mark.anyio
class TestMcpSyncEmptyGuard:
    async def test_mem_context_sync_refuses_when_no_sections(self, tmp_path, monkeypatch):
        project = _seed_project(tmp_path, PROSE_ONLY)
        monkeypatch.chdir(project)
        set_home(monkeypatch, tmp_path / "home")

        out = await mem_context_sync()

        assert "empty" in out.lower()
        assert (project / "CLAUDE.md").read_text(encoding="utf-8") == ORIGINAL_CLAUDE

    async def test_mem_context_sync_still_writes_when_sections_present(self, tmp_path, monkeypatch):
        project = _seed_project(tmp_path, WITH_SECTION)
        monkeypatch.chdir(project)
        set_home(monkeypatch, tmp_path / "home")

        await mem_context_sync()

        assert (project / "CLAUDE.md").read_text(encoding="utf-8") != ORIGINAL_CLAUDE
