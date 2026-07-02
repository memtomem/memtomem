"""Path-disclosure regression pins for the skills / commands / agents context
routes — the #1412 sweep that never finished (dde6fd33 hardened only
mcp-servers).

Two legs, both already fixed for mcp-servers, previously drifted here:

(a) ``_safe_rel`` under a symlinked / case-variant ``project_root`` emitted the
    ABSOLUTE resolved canonical path in ``canonical_path``: the naive per-kind
    copies only tried ``relative_to`` against the bare (unresolved) root, so a
    resolved canonical (``canonical_artifact_dir`` calls ``.resolve()``) fell
    through to the absolute fallback and leaked ``$HOME`` + the OS username to
    the loopback dashboard. Mirrors
    ``TestMcpServersParseBranchPathFree.test_list_symlinked_root_is_path_free``.

(c) The ``rendered_command`` / ``rendered_agent`` parse-error 422 embedded the
    raw ``str(exc)`` whose message carries the absolute source ``Path`` (the
    parsers raise ``... (source: {path})`` / ``missing YAML frontmatter:
    {path}``). Now routed through ``sanitize_diff_reason`` — the #1412 fix class
    for the mcp parse-error 422, here for commands + agents.

(Leg (b) — the mcp delete-skip raw ``str(exc)`` — lives next to the other mcp
delete tests in ``test_web_routes_context_mcp_servers.py``.)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from memtomem.web.routes import context_agents, context_commands, context_skills


def _client_for(router: APIRouter, project_root: Path) -> TestClient:
    """Minimal app exposing one context router with ``project_root`` pinned.

    Mirrors ``TestMcpServersParseBranchPathFree._build_app`` — override
    ``resolve_scope_root`` so the route runs against a real (possibly
    symlinked) root without the selector / eligibility machinery.
    """
    from memtomem.web.routes.context_projects import resolve_scope_root

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[resolve_scope_root] = lambda: project_root
    return TestClient(app)


def _assert_path_free(body: dict, *leaky: Path) -> None:
    """Whole-body assertion: no absolute path (resolved or not) nor ``$HOME``.

    Serializes the entire response so a future shape change that echoes the
    path under a new key is still caught (mirrors the mcp whole-body check).
    """
    blob = json.dumps(body)
    for p in leaky:
        assert str(p) not in blob, body
        assert str(p.resolve()) not in blob, body
    assert str(Path.home()) not in blob, body


# ── (a) symlinked-root ``_safe_rel`` fallback ────────────────────────────


def _seed_skill(root: Path, name: str = "demo") -> None:
    from memtomem.context.skills import SKILL_MANIFEST

    d = root / ".memtomem" / "skills" / name
    d.mkdir(parents=True)
    (d / SKILL_MANIFEST).write_text("# demo skill\n", encoding="utf-8")


def _seed_command(root: Path, name: str = "demo") -> None:
    p = root / ".memtomem" / "commands" / f"{name}.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\nname: demo\ndescription: d\n---\nbody\n", encoding="utf-8")


def _seed_agent(root: Path, name: str = "demo") -> None:
    p = root / ".memtomem" / "agents" / f"{name}.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\nname: demo\ndescription: d\n---\nbody\n", encoding="utf-8")


# kind → (router, list URL, container key, seeder, expected relative path)
_LIST_SPECS: dict[str, tuple[APIRouter, str, str, object, str]] = {
    "skills": (
        context_skills.router,
        "/context/skills",
        "skills",
        _seed_skill,
        ".memtomem/skills/demo",
    ),
    "commands": (
        context_commands.router,
        "/context/commands",
        "commands",
        _seed_command,
        ".memtomem/commands/demo.md",
    ),
    "agents": (
        context_agents.router,
        "/context/agents",
        "agents",
        _seed_agent,
        ".memtomem/agents/demo.md",
    ),
}


@pytest.mark.requires_symlinks
@pytest.mark.parametrize("kind", ["skills", "commands", "agents"])
def test_list_symlinked_root_canonical_path_is_relative(kind: str, tmp_path: Path) -> None:
    router, url, container, seed, expected = _LIST_SPECS[kind]
    real = (tmp_path / "real").resolve()
    real.mkdir()
    seed(real)  # type: ignore[operator]
    link = tmp_path / "link"
    link.symlink_to(real)

    # The route receives the UNRESOLVED symlink as project_root; the engine
    # canonical is ``.resolve()``'d (→ under ``real``), so a bare
    # ``relative_to(link)`` raises ValueError and the naive fallback leaked the
    # absolute resolved path.
    res = _client_for(router, link).get(url)

    assert res.status_code == 200, res.text
    body = res.json()
    item = next(i for i in body[container] if i.get("canonical_path") is not None)
    assert item["canonical_path"] == expected, item
    _assert_path_free(body, real, real / ".memtomem")


# ── (c) rendered parse-error 422 ─────────────────────────────────────────


def test_rendered_command_parse_error_422_is_path_free(tmp_path: Path) -> None:
    from memtomem.context.scope_resolver import canonical_artifact_dir

    p = tmp_path / ".memtomem" / "commands" / "broken.md"
    p.parent.mkdir(parents=True)
    # A path-separator name fails ``validate_name`` → ``CommandParseError`` whose
    # message ends with ``(source: {absolute canonical path})``.
    p.write_text("---\nname: bad/name\ndescription: d\n---\nbody\n", encoding="utf-8")

    res = _client_for(context_commands.router, tmp_path).get("/context/commands/broken/rendered")

    assert res.status_code == 422, res.text
    body = res.json()
    assert body["detail"]["error_kind"] == "parse", body
    canon = canonical_artifact_dir("commands", "project_shared", tmp_path) / "broken.md"
    _assert_path_free(body, canon, canon.parent, tmp_path)
    assert "Parse error" in body["detail"]["message"], body


def test_rendered_agent_parse_error_422_is_path_free(tmp_path: Path) -> None:
    from memtomem.context.scope_resolver import canonical_artifact_dir

    p = tmp_path / ".memtomem" / "agents" / "broken.md"
    p.parent.mkdir(parents=True)
    # Agents REQUIRE frontmatter → ``AgentParseError("missing YAML frontmatter:
    # {absolute canonical path}")``.
    p.write_text("no frontmatter, just a body\n", encoding="utf-8")

    res = _client_for(context_agents.router, tmp_path).get("/context/agents/broken/rendered")

    assert res.status_code == 422, res.text
    body = res.json()
    assert body["detail"]["error_kind"] == "parse", body
    canon = canonical_artifact_dir("agents", "project_shared", tmp_path) / "broken.md"
    _assert_path_free(body, canon, canon.parent, tmp_path)
    assert "Parse error" in body["detail"]["message"], body
