"""MCP parity pins for ADR-0011 context init/generate/sync scope handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.server.tools.context import (
    mem_context_generate,
    mem_context_init,
    mem_context_sync,
)

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


@pytest.mark.anyio
async def test_mem_context_init_implicit_outside_project_warns_and_seeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Implicit init (no scope=) from outside a project must keep pre-PR-E2
    back-compat — warn + seed .memtomem/ here, not return an error.

    Mirrors the CLI gate at ``cli/context_cmd.py:744`` which only refuses
    when ``scope_explicit`` is true.
    """
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    set_home(monkeypatch, tmp_path / "home")

    out = await mem_context_init()

    assert out.startswith("Initialized:")
    assert "warning: no .git or pyproject.toml" in out
    for kind in ("agents", "skills", "commands"):
        assert (bare / ".memtomem" / kind).is_dir()


@pytest.mark.anyio
async def test_mem_context_generate_scope_user_reads_user_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mem_context_generate(scope="user")`` must fan out from the
    ``user`` canonical tier — the CLI ``mm context generate --scope=user``
    already does this (see ``cli/context_cmd.py:963-987``). Without
    ``scope=`` the default ``project_shared`` tier is read, so a
    user-scope canonical agent is invisible.
    """
    project = _make_project(tmp_path, monkeypatch)
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    user_canonical = canonical_artifact_dir("agents", "user", project)
    user_canonical.mkdir(parents=True)
    (user_canonical / "scoped.md").write_text(_clean_agent_body("scoped"), encoding="utf-8")

    # Default (no scope=) reads project_shared and finds nothing — pins the
    # bug that motivated this fix.
    default_out = await mem_context_generate(include="agents")
    assert "Sub-agent fan-out:" not in default_out
    assert not (home / ".claude" / "agents" / "scoped.md").exists()

    # scope="user" picks up the user-tier canonical.
    scoped_out = await mem_context_generate(include="agents", scope="user")
    assert "Sub-agent fan-out:" in scoped_out
    assert (home / ".claude" / "agents" / "scoped.md").is_file()


@pytest.mark.anyio
async def test_mem_context_init_project_shared_privacy_block_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """project_shared Gate A hard-aborts via click.ClickException
    (apply_gate_a in _gate_a.py:171). The MCP handler must catch it and
    surface a ``privacy block:`` message rather than letting it fall
    through to tool_handler as ``internal error``.
    """
    project = _make_project(tmp_path, monkeypatch)
    set_home(monkeypatch, tmp_path / "home")

    leaky_agent = project / ".claude" / "agents"
    leaky_agent.mkdir(parents=True)
    (leaky_agent / "leak.md").write_text(
        "---\nname: leak\ndescription: leak\n---\nuses AKIAIOSFODNN7EXAMPLE\n",
        encoding="utf-8",
    )

    out = await mem_context_init(
        include="agents",
        scope="project_shared",
        confirm_project_shared=True,
    )

    assert out.startswith("privacy block:")
    assert "Gate A" in out
    assert "internal error" not in out.lower()
    assert not (project / ".memtomem" / "agents" / "leak.md").exists()
