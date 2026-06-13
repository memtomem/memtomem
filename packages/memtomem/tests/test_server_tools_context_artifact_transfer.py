"""Real-FS integration pins for the ``mem_context_artifact_transfer`` MCP tool
(A-13 #1283, ADR-0023 §13).

Headless parity for ``mm context copy`` / ``mm context move`` and the web
transfer route — same engine, same gate semantics. These tests exercise the
tool against real two-project filesystem layouts (not mocked) through the
``.git`` + isolated-``HOME`` fixture the CLI transfer tests use
(``test_cli_context_transfer.py``), and pin:

* registry classification: registered via ``@register("context")``, routed by
  ``mem_do``, NOT one of the frozen core-9;
* the full validation-gate set mirrors the CLI dispatch (mcp-servers branch
  gates first, names validated before any path probe);
* ``apply=False`` (the default) never mutates and never gates;
* tier-keyed confirmation gates: ``confirm_project_shared`` (Gate B) and
  ``allow_host_writes`` (host-write gate — Codex design-gate fold);
* destination resolution is registered-``scope_id``-only with the web's
  ``sync_eligible`` eligibility line (paused AND discovery-only refuse);
* Gate A audit attribution: ``surface="mcp_context_artifact_transfer"``;
* engine kwargs pass-through (``surface`` / ``lock_timeout``) for both the
  transfer engine and the mcp-servers copy adapter.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memtomem.context.projects import KnownProjectsStore, ProjectScope, compute_scope_id
from memtomem.server.tool_registry import ACTIONS
from memtomem.server.tools.context import mem_context_artifact_transfer
from memtomem.server.tools.meta import mem_do

_SECRET_LITERAL = "AKIA1234567890ABCDEF"  # AWS-key shape — caught by the privacy scan


@pytest.fixture()
def projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Two initialized project roots + isolated HOME, cwd at project A.

    ``ContextGatewayConfig`` is monkeypatched at the CLI module the MCP
    destination resolver imports its discovery helpers from, so
    ``to_project_scope_id`` resolution reads a tmp ``known_projects.json``
    (pattern from ``test_cli_context_transfer.py``).
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    for proj in (proj_a, proj_b):
        (proj / ".git").mkdir(parents=True)
        (proj / ".memtomem").mkdir()

    kp = tmp_path / "known_projects.json"

    class _FakeCfg:
        known_projects_path = kp
        experimental_claude_projects_scan = False
        auto_display_configured_projects = True

    monkeypatch.setattr("memtomem.cli.context_cmd.ContextGatewayConfig", lambda: _FakeCfg())
    monkeypatch.chdir(proj_a)
    return {"a": proj_a.resolve(), "b": proj_b.resolve(), "home": home.resolve(), "kp": kp}


def _register_b(projects: dict[str, Path]) -> str:
    """Enroll project B in the known-projects store; return its scope_id."""
    KnownProjectsStore(projects["kp"]).add(projects["b"])
    return compute_scope_id(projects["b"])


def _seed_agent(
    projects: dict[str, Path],
    scope: str,
    name: str = "foo",
    root_key: str = "a",
    body: str | None = None,
) -> Path:
    """Write a dir-layout canonical agent; return the artifact dir."""
    if scope == "user":
        base = projects["home"] / ".memtomem" / "agents"
    elif scope == "project_shared":
        base = projects[root_key] / ".memtomem" / "agents"
    elif scope == "project_local":
        base = projects[root_key] / ".memtomem" / "agents.local"
    else:
        raise ValueError(scope)
    artifact_dir = base / name
    artifact_dir.mkdir(parents=True)
    text = body if body is not None else f"---\nname: {name}\ndescription: t\n---\n\nhello\n"
    (artifact_dir / "agent.md").write_text(text, encoding="utf-8")
    return artifact_dir


def _seed_mcp_server(projects: dict[str, Path], root_key: str = "a", name: str = "pg") -> Path:
    store = projects[root_key] / ".memtomem" / "mcp-servers"
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{name}.json"
    path.write_text(
        json.dumps({"command": "npx", "args": ["-y", "srv"]}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


# ── registry classification (#1283 acceptance) ───────────────────────────


def test_action_registered_in_context_category() -> None:
    """Registered via @register outside the frozen core-9; mem_do dispatches
    by the ``mem_``-stripped name."""
    assert "context_artifact_transfer" in ACTIONS
    assert ACTIONS["context_artifact_transfer"].category == "context"
    # Param surface is the documented one (registry parses the Args section).
    assert set(ACTIONS["context_artifact_transfer"].param_docs) == {
        "asset_type",
        "name",
        "mode",
        "from_scope",
        "to_scope",
        "to_project_scope_id",
        "as_name",
        "apply",
        "confirm_project_shared",
        "allow_host_writes",
    }


def test_action_not_in_core_nine() -> None:
    from memtomem.server import _CORE_TOOLS

    assert "mem_context_artifact_transfer" not in _CORE_TOOLS
    assert len(_CORE_TOOLS) == 9  # the frozen default set stays frozen


@pytest.mark.anyio
async def test_mem_do_routes_the_action(projects) -> None:
    out = await mem_do(action="context_artifact_transfer", params={"asset_type": "agents"})
    # The action's own validation message proves the registry dispatch ran.
    assert "mode must be 'copy' or 'move'" in out


# ── validation gates (mirror the CLI dispatch) ───────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"asset_type": "agents", "name": "x"}, "mode must be 'copy' or 'move'"),
        ({"name": "x", "mode": "copy"}, "asset_type is required"),
        ({"asset_type": "agents", "mode": "copy"}, "name is required"),
        (
            {"asset_type": "memory", "name": "x", "mode": "copy", "to_scope": "user"},
            "unsupported asset_type='memory'",
        ),
        (
            {"asset_type": "agents", "name": "x", "mode": "copy", "to_scope": "bogus"},
            "Unknown to_scope='bogus'",
        ),
        (
            {"asset_type": "agents", "name": "x", "mode": "copy", "from_scope": "bogus"},
            "Unknown from_scope='bogus'",
        ),
        ({"asset_type": "agents", "name": "x", "mode": "copy"}, "nothing to do"),
        (
            {
                "asset_type": "agents",
                "name": "x",
                "mode": "copy",
                "to_scope": "user",
                "to_project_scope_id": "p-deadbeef0000",
            },
            "cannot be combined with",
        ),
        (
            {
                "asset_type": "agents",
                "name": "x",
                "mode": "move",
                "to_scope": "user",
                "as_name": "y",
            },
            "as_name is only valid with mode='copy'",
        ),
        (
            {"asset_type": "agents", "name": "../x", "mode": "copy", "to_scope": "user"},
            "error:",
        ),
        (
            {
                "asset_type": "agents",
                "name": "x",
                "mode": "copy",
                "to_scope": "user",
                "as_name": "../y",
            },
            "error:",
        ),
    ],
)
async def test_validation_gates(projects, kwargs, needle) -> None:
    out = await mem_context_artifact_transfer(**kwargs)
    assert needle in out
    assert out.startswith("error:")


# ── dry-run default + within-project transfers ───────────────────────────


@pytest.mark.anyio
async def test_dry_run_default_does_not_mutate(projects) -> None:
    src = _seed_agent(projects, "user")
    out = await mem_context_artifact_transfer(
        asset_type="agents", name="foo", mode="copy", to_scope="project_local"
    )
    assert out.startswith("Plan: copy agents/foo")
    assert "Re-call with apply=True to execute." in out
    assert (src / "agent.md").exists()
    assert not (projects["a"] / ".memtomem" / "agents.local" / "foo").exists()


@pytest.mark.anyio
async def test_copy_apply_keeps_source_and_appends_marker(projects) -> None:
    src = _seed_agent(projects, "user")
    out = await mem_context_artifact_transfer(
        asset_type="agents", name="foo", mode="copy", to_scope="project_local", apply=True
    )
    assert "✓ copied agents/foo: user → project_local" in out
    assert (src / "agent.md").exists()  # copy never touches the source
    assert (projects["a"] / ".memtomem" / "agents.local" / "foo" / "agent.md").exists()
    assert "Appended .gitignore marker" in out
    gitignore = (projects["a"] / ".gitignore").read_text(encoding="utf-8")
    assert ".memtomem/*.local/" in gitignore


@pytest.mark.anyio
async def test_move_apply_consumes_source(projects) -> None:
    src = _seed_agent(projects, "project_local")
    out = await mem_context_artifact_transfer(
        asset_type="agents",
        name="foo",
        mode="move",
        to_scope="user",
        apply=True,
        allow_host_writes=True,
    )
    assert "✓ moved agents/foo: project_local → user" in out
    assert not src.exists()
    assert (projects["home"] / ".memtomem" / "agents" / "foo" / "agent.md").exists()
    # User-tier sync is project-independent — exact engine command, no cd.
    assert "Next: run `mm context sync --scope user`" in out


# ── tier-keyed confirmation gates ────────────────────────────────────────


@pytest.mark.anyio
async def test_project_shared_requires_confirmation(projects) -> None:
    src = _seed_agent(projects, "user")
    blocked = await mem_context_artifact_transfer(
        asset_type="agents", name="foo", mode="move", to_scope="project_shared", apply=True
    )
    assert blocked.startswith("needs confirmation:")
    assert "confirm_project_shared=True" in blocked
    assert src.exists()  # nothing written without the opt-in

    ok = await mem_context_artifact_transfer(
        asset_type="agents",
        name="foo",
        mode="move",
        to_scope="project_shared",
        apply=True,
        confirm_project_shared=True,
    )
    assert "✓ moved agents/foo" in ok
    assert (projects["a"] / ".memtomem" / "agents" / "foo" / "agent.md").exists()


@pytest.mark.anyio
async def test_user_tier_requires_allow_host_writes(projects) -> None:
    """Codex design-gate fold: a user-tier landing is a host write outside any
    project root and must round-trip the same flag the settings surfaces use."""
    src = _seed_agent(projects, "project_shared")
    blocked = await mem_context_artifact_transfer(
        asset_type="agents", name="foo", mode="copy", to_scope="user", apply=True
    )
    assert blocked.startswith("needs confirmation:")
    assert "allow_host_writes=True" in blocked
    assert not (projects["home"] / ".memtomem" / "agents" / "foo").exists()

    ok = await mem_context_artifact_transfer(
        asset_type="agents",
        name="foo",
        mode="copy",
        to_scope="user",
        apply=True,
        allow_host_writes=True,
    )
    assert "✓ copied agents/foo: project_shared → user" in ok
    assert (projects["home"] / ".memtomem" / "agents" / "foo" / "agent.md").exists()
    assert src.exists()


@pytest.mark.anyio
async def test_user_tier_dry_run_is_ungated_and_names_the_flag(projects) -> None:
    _seed_agent(projects, "project_shared")
    out = await mem_context_artifact_transfer(
        asset_type="agents", name="foo", mode="copy", to_scope="user"
    )
    assert out.startswith("Plan: copy agents/foo")
    assert "Re-call with apply=True and allow_host_writes=True to execute." in out
    assert not (projects["home"] / ".memtomem" / "agents" / "foo").exists()


# ── cross-project destination resolution + eligibility ───────────────────


@pytest.mark.anyio
async def test_cross_project_move_round_trip(projects) -> None:
    src = _seed_agent(projects, "project_shared", root_key="a")
    sid = _register_b(projects)
    out = await mem_context_artifact_transfer(
        asset_type="agents",
        name="foo",
        mode="move",
        to_scope="project_shared",
        to_project_scope_id=sid,
        apply=True,
        confirm_project_shared=True,
    )
    assert "✓ moved agents/foo: project_shared → project_shared" in out
    assert not src.exists()
    assert (projects["b"] / ".memtomem" / "agents" / "foo" / "agent.md").exists()
    # Engine's exact destination-project follow-up (cd-prefixed).
    assert "mm context sync --scope project_shared" in out
    assert str(projects["b"]) in out


@pytest.mark.anyio
async def test_omitted_to_scope_keeps_source_tier_cross_project(projects) -> None:
    _seed_agent(projects, "project_shared", root_key="a")
    sid = _register_b(projects)
    out = await mem_context_artifact_transfer(
        asset_type="agents", name="foo", mode="copy", to_project_scope_id=sid
    )
    assert out.startswith("Plan: copy agents/foo")
    assert "to   project_shared" in out
    assert str(projects["b"]) in out


@pytest.mark.anyio
async def test_user_source_with_to_project_demands_explicit_tier(projects) -> None:
    _seed_agent(projects, "user")
    sid = _register_b(projects)
    out = await mem_context_artifact_transfer(
        asset_type="agents", name="foo", mode="copy", to_project_scope_id=sid
    )
    assert out.startswith("error:")
    assert "lives at the user tier" in out


@pytest.mark.anyio
async def test_unknown_scope_id_errors_with_registry_hint(projects) -> None:
    _seed_agent(projects, "project_shared")
    out = await mem_context_artifact_transfer(
        asset_type="agents",
        name="foo",
        mode="copy",
        to_scope="project_shared",
        to_project_scope_id="p-deadbeef0000",
    )
    assert out.startswith("error: unknown to_project_scope_id")
    assert "mm context projects list" in out


@pytest.mark.anyio
async def test_paused_destination_refuses_with_resume_hint(projects) -> None:
    src = _seed_agent(projects, "project_shared")
    sid = _register_b(projects)
    KnownProjectsStore(projects["kp"]).set_enabled_by_scope_id(sid, False)
    out = await mem_context_artifact_transfer(
        asset_type="agents",
        name="foo",
        mode="move",
        to_scope="project_shared",
        to_project_scope_id=sid,
        apply=True,
        confirm_project_shared=True,
    )
    assert out.startswith("refused:")
    assert "paused" in out
    assert f"mm context projects resume {sid}" in out
    assert src.exists()


@pytest.mark.anyio
async def test_discovery_only_destination_refuses_with_enroll_hint(
    projects, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The web-strict eligibility line (ADR-0023 §13): a scan-discovered,
    never-enrolled scope is selectable by scope_id but refuses — the CLI's
    select-is-consent looseness does not extend to headless agents."""
    _seed_agent(projects, "project_shared")
    ghost = ProjectScope(
        scope_id="p-cafecafe0000",
        label="ghost",
        root=projects["b"],
        tier="project",
        sources=("claude-projects",),
        sync_eligible=False,
    )
    monkeypatch.setattr(
        "memtomem.cli.context_cmd._projects_discover", lambda cfg, cwd=None: [ghost]
    )
    out = await mem_context_artifact_transfer(
        asset_type="agents",
        name="foo",
        mode="copy",
        to_scope="project_shared",
        to_project_scope_id="p-cafecafe0000",
    )
    assert out.startswith("refused:")
    assert "discovery-only" in out
    assert "mm context projects add" in out


@pytest.mark.anyio
async def test_destination_without_store_refuses_with_init_hint(projects, tmp_path: Path) -> None:
    _seed_agent(projects, "project_shared")
    bare = tmp_path / "bare"
    (bare / ".git").mkdir(parents=True)  # a project, but never `mm context init`ed
    KnownProjectsStore(projects["kp"]).add(bare)
    out = await mem_context_artifact_transfer(
        asset_type="agents",
        name="foo",
        mode="copy",
        to_scope="project_local",
        to_project_scope_id=compute_scope_id(bare),
    )
    assert out.startswith("refused:")
    assert "no .memtomem/ store" in out
    assert "mm context init" in out


@pytest.mark.anyio
async def test_destination_collision_refuses(projects) -> None:
    _seed_agent(projects, "project_shared", root_key="a")
    sid = _register_b(projects)
    _seed_agent(projects, "project_shared", root_key="b")  # dst already present
    out = await mem_context_artifact_transfer(
        asset_type="agents",
        name="foo",
        mode="copy",
        to_scope="project_shared",
        to_project_scope_id=sid,
        apply=True,
        confirm_project_shared=True,
    )
    assert out.startswith("refused:")
    assert "destination already exists" in out


@pytest.mark.anyio
async def test_missing_source_is_clean_error(projects) -> None:
    out = await mem_context_artifact_transfer(
        asset_type="agents", name="ghost", mode="copy", to_scope="project_local", apply=True
    )
    assert out.startswith("error:")
    assert "ghost" in out


# ── Gate A (privacy scan) + audit attribution ────────────────────────────


@pytest.mark.anyio
async def test_gate_a_blocks_project_shared_landing(projects) -> None:
    src_dir = _seed_agent(
        projects,
        "project_local",
        body=f"---\nname: foo\ndescription: t\n---\n\n{_SECRET_LITERAL}\n",
    )
    sid = _register_b(projects)
    out = await mem_context_artifact_transfer(
        asset_type="agents",
        name="foo",
        mode="move",
        to_scope="project_shared",
        to_project_scope_id=sid,
        apply=True,
        confirm_project_shared=True,
    )
    assert out.startswith("privacy block:")
    assert (src_dir / "agent.md").is_file()  # move rolled back
    assert not (projects["b"] / ".memtomem" / "agents" / "foo").exists()  # zero residue


@pytest.mark.anyio
async def test_gate_a_audit_attributes_mcp_surface(
    projects, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1283 acceptance: MCP-driven transfers reach the privacy audit log as
    ``mcp_context_artifact_transfer``, not a CLI/web literal. Spy at the
    sync-side chokepoint every staging scan funnels through (#1246 pattern)."""
    from memtomem.privacy import WriteGuardResult

    surfaces: list[str] = []

    def spy(content_text, *, surface, **kw):
        surfaces.append(surface)
        return WriteGuardResult("pass", [])

    monkeypatch.setattr("memtomem.context.privacy_scan.privacy.enforce_write_guard", spy)

    _seed_agent(projects, "user")
    out = await mem_context_artifact_transfer(
        asset_type="agents",
        name="foo",
        mode="copy",
        to_scope="project_shared",
        apply=True,
        confirm_project_shared=True,
    )
    assert "✓ copied agents/foo" in out
    assert surfaces, "Gate A never ran"
    assert set(surfaces) == {"mcp_context_artifact_transfer"}


# ── engine kwargs pass-through (monkeypatch pin) ─────────────────────────


@pytest.mark.anyio
async def test_engine_kwargs_pass_through(projects, monkeypatch: pytest.MonkeyPatch) -> None:
    """Campaign lesson: monkeypatched-engine tests must pin pass-through
    kwargs, or a dropped ``surface=``/``lock_timeout=`` ships silently."""
    from memtomem.context.transfer import TransferResult

    captured: dict = {}

    def fake_transfer(kind, name, **kwargs):
        captured.update(kwargs, kind=kind, name=name)
        return TransferResult(
            kind=kind,
            name=name,
            dst_name=kwargs["new_name"] or name,
            mode=kwargs["mode"],
            from_scope="project_shared",
            to_scope=kwargs["to_scope"],
            src_project_root=kwargs["src_project_root"],
            dst_project_root=kwargs["dst_project_root"],
            src_path=Path("/x/src"),
            dst_path=Path("/x/dst"),
            layout="dir",
            transferred=False,
        )

    monkeypatch.setattr("memtomem.context.transfer.transfer_artifact", fake_transfer)
    out = await mem_context_artifact_transfer(
        asset_type="agents",
        name="foo",
        mode="copy",
        to_scope="project_local",
        as_name="bar",
    )
    assert out.startswith("Plan: copy agents/foo as bar")
    assert captured["surface"] == "mcp_context_artifact_transfer"
    assert captured["lock_timeout"] == 30.0
    assert captured["mode"] == "copy"
    assert captured["new_name"] == "bar"
    assert captured["apply_"] is False


@pytest.mark.anyio
async def test_mcp_copy_adapter_kwargs_pass_through(
    projects, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.context.mcp_servers_copy import McpServerCopyResult

    captured: dict = {}

    def fake_copy(name, **kwargs):
        captured.update(kwargs, name=name)
        return McpServerCopyResult(
            kind="mcp-servers",
            name=name,
            dst_name=name,
            mode="copy",
            from_scope="project_shared",
            to_scope="project_shared",
            src_project_root=kwargs["src_project_root"],
            dst_project_root=kwargs["dst_project_root"],
            src_path=Path("/x/src.json"),
            dst_path=Path("/x/dst.json"),
            layout="flat",
            transferred=False,
        )

    monkeypatch.setattr("memtomem.context.mcp_servers_copy.copy_mcp_server", fake_copy)
    sid = _register_b(projects)
    out = await mem_context_artifact_transfer(
        asset_type="mcp-servers", name="pg", mode="copy", to_project_scope_id=sid
    )
    assert out.startswith("Plan: copy mcp-servers/pg")
    assert captured["surface"] == "mcp_context_artifact_transfer"
    assert captured["lock_timeout"] == 30.0
    assert captured["apply_"] is False


# ── mcp-servers branch (A-12 adapter) ────────────────────────────────────


@pytest.mark.anyio
async def test_mcp_copy_round_trip(projects) -> None:
    src = _seed_mcp_server(projects)
    sid = _register_b(projects)
    out = await mem_context_artifact_transfer(
        asset_type="mcp-servers",
        name="pg",
        mode="copy",
        to_project_scope_id=sid,
        apply=True,
        confirm_project_shared=True,
    )
    assert "✓ copied mcp-servers/pg: project_shared → project_shared" in out
    dst = projects["b"] / ".memtomem" / "mcp-servers" / "pg.json"
    assert dst.read_bytes() == src.read_bytes()
    # Prose follow-up (web-only fan-out), not a runnable sync command — and
    # no layout noise for the single-layout kind.
    assert "Next: fan out at the destination from its web panel" in out
    assert "(flat layout)" not in out


@pytest.mark.anyio
async def test_mcp_dry_run_names_required_flags(projects) -> None:
    _seed_mcp_server(projects)
    sid = _register_b(projects)
    out = await mem_context_artifact_transfer(
        asset_type="mcp-servers", name="pg", mode="copy", to_project_scope_id=sid
    )
    assert out.startswith("Plan: copy mcp-servers/pg")
    assert "Re-call with apply=True and confirm_project_shared=True to execute." in out
    assert "After apply, fan out at the destination from its web panel" in out
    assert not (projects["b"] / ".memtomem" / "mcp-servers").exists()


@pytest.mark.anyio
async def test_mcp_gate_b_still_applies(projects) -> None:
    _seed_mcp_server(projects)
    sid = _register_b(projects)
    out = await mem_context_artifact_transfer(
        asset_type="mcp-servers", name="pg", mode="copy", to_project_scope_id=sid, apply=True
    )
    assert out.startswith("needs confirmation:")
    assert not (projects["b"] / ".memtomem" / "mcp-servers").exists()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"mode": "move"}, "mcp-servers support copy only"),
        ({"mode": "copy", "as_name": "pg2"}, "as_name is not supported for mcp-servers"),
        (
            {"mode": "copy", "to_scope": "project_local"},
            "not valid for mcp-servers",
        ),
        (
            {"mode": "copy", "from_scope": "user"},
            "not valid for mcp-servers",
        ),
    ],
)
async def test_mcp_branch_gates(projects, kwargs, needle) -> None:
    _seed_mcp_server(projects)
    sid = _register_b(projects)
    out = await mem_context_artifact_transfer(
        asset_type="mcp-servers", name="pg", to_project_scope_id=sid, **kwargs
    )
    assert out.startswith("error:")
    assert needle in out


@pytest.mark.anyio
async def test_mcp_requires_to_project(projects) -> None:
    _seed_mcp_server(projects)
    out = await mem_context_artifact_transfer(asset_type="mcp-servers", name="pg", mode="copy")
    assert out.startswith("error:")
    assert "cross-project only" in out


@pytest.mark.anyio
async def test_mcp_same_project_destination_rejected(projects) -> None:
    _seed_mcp_server(projects)
    KnownProjectsStore(projects["kp"]).add(projects["a"])
    out = await mem_context_artifact_transfer(
        asset_type="mcp-servers",
        name="pg",
        mode="copy",
        to_project_scope_id=compute_scope_id(projects["a"]),
    )
    assert out.startswith("error:")
    assert "resolves to the source project" in out
