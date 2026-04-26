"""MCP-tool gate threading: ``mem_context_sync`` / ``mem_context_generate``
forward ``allow_host_writes`` into ``generate_all_settings``.

The full gate semantics are covered in test_context_settings.py; this file
just pins the wiring so a future revert that forgets to thread the param
fails CI immediately (review item 1 on PR #484).
"""

from __future__ import annotations

import json

import pytest

from memtomem.context.settings import CANONICAL_SETTINGS_FILE
from memtomem.server.tools.context import mem_context_generate, mem_context_sync


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """Redirect HOME so any Claude write lands in a temp dir."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    return fake_home


def _setup_project(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    canonical = project / CANONICAL_SETTINGS_FILE
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Write",
                            "hooks": [{"type": "command", "command": "echo test"}],
                        }
                    ]
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    return project


@pytest.mark.anyio
async def test_mem_context_sync_refuses_host_write_by_default(claude_home, tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    out = await mem_context_sync(include="settings")
    assert "needs confirmation" in out
    assert "claude_settings" in out
    target = claude_home / ".claude" / "settings.json"
    assert not target.exists()


@pytest.mark.anyio
async def test_mem_context_sync_accepts_allow_host_writes(claude_home, tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    out = await mem_context_sync(include="settings", allow_host_writes=True)
    assert "needs confirmation" not in out
    target = claude_home / ".claude" / "settings.json"
    assert target.is_file()


@pytest.mark.anyio
async def test_mem_context_generate_refuses_host_write_by_default(
    claude_home, tmp_path, monkeypatch
):
    _setup_project(tmp_path, monkeypatch)
    out = await mem_context_generate(include="settings")
    assert "needs confirmation" in out
    target = claude_home / ".claude" / "settings.json"
    assert not target.exists()


@pytest.mark.anyio
async def test_mem_context_generate_accepts_allow_host_writes(claude_home, tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)
    out = await mem_context_generate(include="settings", allow_host_writes=True)
    assert "needs confirmation" not in out
    target = claude_home / ".claude" / "settings.json"
    assert target.is_file()
