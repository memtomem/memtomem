"""MCP parity pins for ADR-0011 scope handling in ``mem_context_diff``."""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.server.tools.context import mem_context_diff

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
async def test_mem_context_diff_artifact_only_skips_settings_scope_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lazy settings-scope resolve for ``mem_context_diff``. Pre-#887 the handler called
    ``_resolve_mcp_scope()`` eagerly with no argument inside the
    ``if "settings" in inc:`` block — which still meant that a poisoned
    settings resolver couldn't be sidestepped via artifact-only diffs.
    After the fix, omitting ``settings`` from ``include`` must never reach
    the settings scope resolver, AND the artifact diff must compare the
    requested ``scope`` tier (not the default ``project_shared``).
    """
    project = _make_project(tmp_path, monkeypatch)
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    canonical = canonical_artifact_dir("agents", "user", project)
    canonical.mkdir(parents=True)
    (canonical / "ok.md").write_text(_clean_agent_body("ok"), encoding="utf-8")
    runtime = home / ".claude" / "agents"
    runtime.mkdir(parents=True)
    (runtime / "ok.md").write_text(_clean_agent_body("ok"), encoding="utf-8")

    from memtomem.server.tools import context as context_mod

    def _boom(*_a: object, **_kw: object) -> str:
        raise RuntimeError("settings scope resolver poisoned for test")

    monkeypatch.setattr(context_mod, "_resolve_mcp_scope", _boom)

    out = await mem_context_diff(include="agents", scope="user")
    # Settings resolver never reached.
    assert "internal error" not in out.lower()
    # And the artifact diff actually picked up the user-tier canonical
    # rather than defaulting to project_shared (which has nothing).
    assert "Sub-agents:" in out
    assert "ok" in out


@pytest.mark.anyio
async def test_mem_context_diff_scope_user_reads_user_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mem_context_diff(include="agents", scope="user")`` must compare the
    USER-tier canonical against the user-tier runtime, the way ``mm context
    sync --scope user`` routes it. Pre-fix
    the diff passed no ``scope=`` argument to ``diff_agents``, so the
    handler silently used the default ``project_shared`` tier and reported
    "No sub-agents to compare." for user-tier installations. Codex flagged
    this on PR #920.
    """
    project = _make_project(tmp_path, monkeypatch)
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    user_canonical = canonical_artifact_dir("agents", "user", project)
    user_canonical.mkdir(parents=True)
    (user_canonical / "scoped.md").write_text(_clean_agent_body("scoped"), encoding="utf-8")
    runtime = home / ".claude" / "agents"
    runtime.mkdir(parents=True)
    (runtime / "scoped.md").write_text(_clean_agent_body("scoped"), encoding="utf-8")

    # Default (no scope=) reads project_shared and finds nothing — pins the
    # bug Codex caught.
    default_out = await mem_context_diff(include="agents")
    assert "No sub-agents to compare." in default_out

    # scope="user" picks up the user-tier canonical and reports it across
    # the registered runtimes (status — "in sync" / "out of sync" /
    # "missing target" — depends on the runtime-side generator output,
    # which is not the scope axis under test here).
    scoped_out = await mem_context_diff(include="agents", scope="user")
    assert "Sub-agents:" in scoped_out
    assert "scoped" in scoped_out


@pytest.mark.anyio
async def test_mem_context_diff_settings_scope_passes_through_explicit_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mem_context_diff(include="settings", scope=...)`` must thread the
    caller's explicit scope through to ``_resolve_mcp_scope`` rather than
    calling it with no arguments (the pre-#887 bug). Without this, MCP
    callers cannot target the settings diff at a non-default tier — MCP
    has no cwd to infer from, unlike the CLI.
    """
    _make_project(tmp_path, monkeypatch)
    set_home(monkeypatch, tmp_path / "home")

    from memtomem.server.tools import context as context_mod

    captured: list[str | None] = []

    def _capture(scope: str | None = None) -> str:
        captured.append(scope)
        return "user"

    monkeypatch.setattr(context_mod, "_resolve_mcp_scope", _capture)

    await mem_context_diff(include="settings", scope="user")
    assert captured == ["user"]

    captured.clear()
    await mem_context_diff(include="settings", scope="project_shared")
    assert captured == ["project_shared"]

    # Empty / unset scope must collapse to None so the resolver applies its
    # config/env default.
    captured.clear()
    await mem_context_diff(include="settings")
    assert captured == [None]
