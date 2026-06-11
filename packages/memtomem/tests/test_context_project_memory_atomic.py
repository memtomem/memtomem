"""Mechanism pins for #1247 id 19 — project-memory writes are atomic.

``context/_atomic.py`` states the invariant "Every gateway write site funnels
through the helpers in this module", and every artifact fan-out family
(agents, commands, skills, settings, MCP servers, web mutators) honors it —
but the project-memory fan-out (CLAUDE.md / GEMINI.md / ...) and the canonical
``.memtomem/context.md`` writes used bare ``Path.write_text`` on the CLI and
MCP surfaces. A crash mid-write truncates the target; for ``context.md`` the
loss is not even regenerable.

These tests pin the MECHANISM (the write goes through ``atomic_write_text``
with mode ``0o644``) rather than simulating a crash at the command level —
the helper's own crash semantics are already pinned by
``test_context_atomic.py``; patching ``os.write`` here would be global and
brittle.

Patch-site note: ``cli.context_cmd`` binds the helper at module import time
(patch the bound name); the MCP tools import it function-locally at call time
(patch the source module attribute). The spy fixture patches both.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.context._atomic import atomic_write_text as _real_atomic_write_text
from memtomem.context.parser import CONTEXT_FILENAME

from .helpers import set_home


@pytest.fixture
def atomic_spy(monkeypatch):
    """Wrap-spy on ``atomic_write_text`` recording ``(path, mode)`` calls."""
    calls: list[tuple[Path, int]] = []

    def spy(path, text, mode=0o600, encoding="utf-8"):
        calls.append((Path(path), mode))
        _real_atomic_write_text(path, text, mode=mode, encoding=encoding)

    import memtomem.cli.context_cmd as ctx_cmd
    import memtomem.context._atomic as atomic_mod

    monkeypatch.setattr(ctx_cmd, "atomic_write_text", spy)
    monkeypatch.setattr(atomic_mod, "atomic_write_text", spy)
    return calls


def _spied_paths(calls: list[tuple[Path, int]]) -> list[Path]:
    return [p for p, _mode in calls]


def _assert_mode_644(calls: list[tuple[Path, int]], target: Path) -> None:
    modes = {mode for p, mode in calls if p == target}
    assert modes == {0o644}, (
        f"{target} should be written 0o644 (readable project file), got {modes}"
    )


# ── CLI surface ───────────────────────────────────────────────────────


def _runner_in_project(tmp_path, monkeypatch):
    """Hermetic CliRunner in a seeded project (test_context_cli_exit_scope idiom)."""
    from memtomem.cli import _bootstrap

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "atomic-pin"\nversion = "0"\n', encoding="utf-8"
    )
    set_home(monkeypatch, home)
    monkeypatch.delenv("MEMTOMEM_HOOKS__TARGET_SCOPE", raising=False)
    monkeypatch.setattr(_bootstrap, "_CONFIG_PATH", home / ".memtomem" / "config.json")
    monkeypatch.chdir(project)
    return CliRunner(), project


def _ctx_with_sections(root: Path) -> None:
    ctx = root / ".memtomem" / "context.md"
    ctx.parent.mkdir(parents=True, exist_ok=True)
    ctx.write_text(
        "# Project Context\n\n## Project\n- Name: atomic-pin\n\n## Rules\n- keep\n",
        encoding="utf-8",
    )


class TestCliProjectMemoryAtomic:
    def test_init_writes_context_md_atomically(self, tmp_path, monkeypatch, atomic_spy):
        runner, project = _runner_in_project(tmp_path, monkeypatch)

        r = runner.invoke(cli, ["context", "init"])

        assert r.exit_code == 0, r.output
        ctx_path = project / ".memtomem" / "context.md"
        assert ctx_path.is_file()
        assert ctx_path in _spied_paths(atomic_spy)
        _assert_mode_644(atomic_spy, ctx_path)

    def test_generate_writes_project_memory_atomically(self, tmp_path, monkeypatch, atomic_spy):
        runner, project = _runner_in_project(tmp_path, monkeypatch)
        _ctx_with_sections(project)

        r = runner.invoke(cli, ["context", "generate", "--agent", "claude"])

        assert r.exit_code == 0, r.output
        claude_md = project / "CLAUDE.md"
        assert claude_md.is_file()
        assert "atomic-pin" in claude_md.read_text(encoding="utf-8")
        assert claude_md in _spied_paths(atomic_spy)
        _assert_mode_644(atomic_spy, claude_md)

    def test_sync_writes_project_memory_atomically(self, tmp_path, monkeypatch, atomic_spy):
        runner, project = _runner_in_project(tmp_path, monkeypatch)
        _ctx_with_sections(project)
        # sync only refreshes DETECTED agent files — seed a stale one.
        (project / "CLAUDE.md").write_text("stale sentinel\n", encoding="utf-8")

        r = runner.invoke(cli, ["context", "sync"])

        assert r.exit_code == 0, r.output
        claude_md = project / "CLAUDE.md"
        text = claude_md.read_text(encoding="utf-8")
        assert "stale sentinel" not in text
        assert "atomic-pin" in text
        assert claude_md in _spied_paths(atomic_spy)
        _assert_mode_644(atomic_spy, claude_md)


# ── MCP surface ───────────────────────────────────────────────────────


class TestMcpProjectMemoryAtomic:
    def _setup_project(self, tmp_path, monkeypatch) -> Path:
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".git").mkdir()
        set_home(monkeypatch, tmp_path / "home")
        monkeypatch.chdir(project)
        return project

    @pytest.mark.anyio
    async def test_init_writes_context_md_atomically(self, tmp_path, monkeypatch, atomic_spy):
        from memtomem.server.tools.context import mem_context_init

        project = self._setup_project(tmp_path, monkeypatch)

        out = await mem_context_init()

        assert f"Created {CONTEXT_FILENAME}" in out
        ctx_path = project / ".memtomem" / "context.md"
        assert ctx_path.is_file()
        assert ctx_path in _spied_paths(atomic_spy)
        _assert_mode_644(atomic_spy, ctx_path)

    @pytest.mark.anyio
    async def test_generate_writes_project_memory_atomically(
        self, tmp_path, monkeypatch, atomic_spy
    ):
        from memtomem.server.tools.context import mem_context_generate

        project = self._setup_project(tmp_path, monkeypatch)
        _ctx_with_sections(project)

        out = await mem_context_generate(agent="claude")

        assert "claude" in out
        claude_md = project / "CLAUDE.md"
        assert claude_md.is_file()
        assert claude_md in _spied_paths(atomic_spy)
        _assert_mode_644(atomic_spy, claude_md)

    @pytest.mark.anyio
    async def test_sync_writes_project_memory_atomically(self, tmp_path, monkeypatch, atomic_spy):
        from memtomem.server.tools.context import mem_context_sync

        project = self._setup_project(tmp_path, monkeypatch)
        _ctx_with_sections(project)
        (project / "CLAUDE.md").write_text("stale sentinel\n", encoding="utf-8")

        out = await mem_context_sync()

        assert "claude" in out
        claude_md = project / "CLAUDE.md"
        text = claude_md.read_text(encoding="utf-8")
        assert "stale sentinel" not in text
        assert claude_md in _spied_paths(atomic_spy)
        _assert_mode_644(atomic_spy, claude_md)
