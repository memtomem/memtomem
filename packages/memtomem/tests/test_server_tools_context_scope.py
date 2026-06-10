"""MCP parity pins for ADR-0011 context init/generate/sync scope handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.server.tools.context import (
    mem_context_diff,
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
async def test_mem_context_generate_artifact_only_skips_settings_scope_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifact-only generate must not eagerly resolve the settings scope.

    ``_resolve_mcp_scope`` builds ``Mem2MemConfig`` and applies env/file
    overrides — if an unrelated override is broken, the whole call would
    fail before touching artifacts. Pin: monkeypatch the helper to raise,
    then call generate with ``include="agents"``; the artifact path must
    still complete.
    """
    project = _make_project(tmp_path, monkeypatch)
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    canonical = canonical_artifact_dir("agents", "user", project)
    canonical.mkdir(parents=True)
    (canonical / "ok.md").write_text(_clean_agent_body("ok"), encoding="utf-8")

    from memtomem.server.tools import context as context_mod

    def _boom(*_a: object, **_kw: object) -> str:
        raise RuntimeError("settings scope resolver poisoned for test")

    monkeypatch.setattr(context_mod, "_resolve_mcp_scope", _boom)

    out = await mem_context_generate(include="agents", scope="user")
    assert "Sub-agent fan-out:" in out
    assert (home / ".claude" / "agents" / "ok.md").is_file()


@pytest.mark.anyio
async def test_mem_context_sync_artifact_only_skips_settings_scope_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same regression pin as ``mem_context_generate`` but for ``mem_context_sync``."""
    project = _make_project(tmp_path, monkeypatch)
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    canonical = canonical_artifact_dir("agents", "user", project)
    canonical.mkdir(parents=True)
    (canonical / "ok.md").write_text(_clean_agent_body("ok"), encoding="utf-8")

    from memtomem.server.tools import context as context_mod

    def _boom(*_a: object, **_kw: object) -> str:
        raise RuntimeError("settings scope resolver poisoned for test")

    monkeypatch.setattr(context_mod, "_resolve_mcp_scope", _boom)

    out = await mem_context_sync(include="agents", scope="user")
    assert "Sub-agent fan-out:" in out
    assert (home / ".claude" / "agents" / "ok.md").is_file()


@pytest.mark.anyio
async def test_mem_context_init_overwrite_does_not_clobber_context_md(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``overwrite=True`` is for artifact imports only — it must NOT
    rewrite an existing ``.memtomem/context.md``. The CLI keeps
    context.md rewrite behind a separate confirmation prompt
    (``cli/context_cmd.py:789-798``) that defaults to "No"; the MCP
    surface mirrors that with the explicit ``overwrite_context_md``
    flag (default False).
    """
    project = _make_project(tmp_path, monkeypatch)
    set_home(monkeypatch, tmp_path / "home")

    ctx_path = project / ".memtomem" / "context.md"
    ctx_path.parent.mkdir(parents=True)
    handcrafted = "# hand-edited\n\nimportant project memory\n"
    ctx_path.write_text(handcrafted, encoding="utf-8")

    # overwrite=True targets artifact imports only — context.md is preserved.
    out = await mem_context_init(include="agents", overwrite=True)
    assert "skipped" in out and "context.md rewrite" in out
    assert ctx_path.read_text(encoding="utf-8") == handcrafted

    # Explicit overwrite_context_md=True is required to replace it.
    out = await mem_context_init(overwrite_context_md=True)
    assert ctx_path.read_text(encoding="utf-8") != handcrafted


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


@pytest.mark.anyio
async def test_mem_context_diff_artifact_only_skips_settings_scope_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same regression pin as ``mem_context_generate`` / ``mem_context_sync``
    but for ``mem_context_diff``. Pre-#887 the handler called
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
    USER-tier canonical against the user-tier runtime, mirroring how
    ``mem_context_generate`` / ``mem_context_sync`` route their scope. Pre-fix
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
    # config/env default — matches the lazy-resolve idiom in
    # ``mem_context_generate`` at lines 520-524.
    captured.clear()
    await mem_context_diff(include="settings")
    assert captured == [None]


@pytest.mark.anyio
async def test_mem_context_init_uses_mcp_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate A audit attribution (#1229): imports driven by the MCP tool must
    reach the privacy audit log as ``mcp_context_init``, not the CLI literal."""
    from memtomem.privacy import WriteGuardResult

    _make_project(tmp_path, monkeypatch)
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    agents = home / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "agt.md").write_text(_clean_agent_body("agt"), encoding="utf-8")
    skill = home / ".claude" / "skills" / "sk"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: sk\n---\nbody\n", encoding="utf-8")
    cmds = home / ".claude" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "cmd.md").write_text("---\nname: cmd\n---\nbody\n", encoding="utf-8")

    surfaces: list[str] = []

    def spy(content_text, *, surface, **kw):
        surfaces.append(surface)
        return WriteGuardResult("pass", [])

    monkeypatch.setattr("memtomem.context._gate_a.privacy.enforce_write_guard", spy)

    out = await mem_context_init(include="skills,agents,commands", scope="user")

    assert out.startswith("Imported") or "Imported" in out
    assert surfaces, "Gate A never ran"
    assert set(surfaces) == {"mcp_context_init"}
