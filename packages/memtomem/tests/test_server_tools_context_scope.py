"""MCP parity pins for ADR-0011 context init/sync scope handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.server.tools.context import mem_context_init, mem_context_sync

from .helpers import set_home


def _make_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.chdir(project)
    return project


def _clean_agent_body(name: str) -> str:
    return f"---\nname: {name}\ndescription: example\n---\nbody\n"


@pytest.mark.anyio
async def test_mem_context_init_scope_user_seeds_user_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path, monkeypatch)
    home = tmp_path / "home"
    set_home(monkeypatch, home)

    out = await mem_context_init(scope="user")

    assert out.startswith("Initialized:")
    for kind in ("agents", "skills", "commands"):
        assert (home / ".memtomem" / kind).is_dir()
        assert not (project / ".memtomem" / kind).exists()
    assert not (project / ".memtomem" / "context.md").exists()


@pytest.mark.anyio
async def test_mem_context_init_project_shared_requires_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path, monkeypatch)
    set_home(monkeypatch, tmp_path / "home")

    out = await mem_context_init(scope="project_shared")

    assert out.startswith("needs confirmation:")
    assert not (project / ".memtomem" / "agents").exists()


@pytest.mark.anyio
async def test_mem_context_sync_scope_user_reads_user_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path, monkeypatch)
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    canonical = canonical_artifact_dir("agents", "user", project)
    canonical.mkdir(parents=True)
    (canonical / "ok.md").write_text(_clean_agent_body("ok"), encoding="utf-8")

    out = await mem_context_sync(include="agents", scope="user")

    assert "Sub-agent fan-out:" in out
    assert (home / ".claude" / "agents" / "ok.md").is_file()
    assert not (project / ".claude" / "agents" / "ok.md").exists()
