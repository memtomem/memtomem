"""HTTP-layer tests for the multi-project context-gateway routes.

Covers:
- ``GET /api/context/projects`` shape with cwd-only and cwd+known scopes.
- ``?project_scope_id=`` / ``?scope_id=`` queries on scoped routes.
- ``POST /api/context/known-projects`` validation + marker warning.
- ``DELETE /api/context/known-projects/{scope_id}`` success / 404.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import ContextGatewayConfig, Mem2MemConfig
from memtomem.context.projects import compute_scope_id
from memtomem.web.app import create_app
from .helpers import set_home


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def cwd_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox HOME; return the cwd project root with a .claude marker."""
    set_home(monkeypatch, tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".claude").mkdir()
    return cwd


@pytest.fixture
def known_projects_path(tmp_path: Path) -> Path:
    return tmp_path / "kp.json"


@pytest.fixture
def app(cwd_root: Path, known_projects_path: Path):
    """Create_app with state populated to simulate a live mm web server."""
    application = create_app(lifespan=None, mode="dev")
    application.state.project_root = cwd_root
    application.state.storage = AsyncMock()
    # Real Mem2MemConfig with overridden context_gateway path so the route
    # writes go to a tmp file instead of the user's real ~/.memtomem/.
    config = Mem2MemConfig()
    config.context_gateway = ContextGatewayConfig(
        known_projects_path=known_projects_path,
        experimental_claude_projects_scan=False,
    )
    application.state.config = config
    application.state.search_pipeline = None
    application.state.index_engine = None
    application.state.embedder = None
    application.state.dedup_scanner = None
    application.state.last_reload_error = None
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── GET /context/projects ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_projects_cwd_only(client) -> None:
    resp = await client.get("/api/context/projects")
    assert resp.status_code == 200
    data = resp.json()
    assert "scopes" in data
    assert len(data["scopes"]) == 1
    scope = data["scopes"][0]
    assert scope["project_scope_id"] == scope["scope_id"]
    assert scope["label"] == "Server CWD"
    assert scope["sources"] == ["server-cwd"]
    assert scope["tier"] == "project"
    assert scope["missing"] is False
    assert scope["experimental"] is False
    # Sync-enrollment fields: the server cwd is always sync-eligible (it cannot
    # be paused), and ``enabled`` defaults True for a scope with no known entry.
    assert scope["enabled"] is True
    assert scope["sync_eligible"] is True
    # cwd_root has a .claude marker but no .memtomem store → reported stale.
    assert scope["stale"] is True
    # Counts and runtime coverage are both opt-in now (ADR-0021 PR2): the
    # default response omits them (``null``, distinct from a real zero / empty
    # list) so the project list stays cheap.
    assert scope["counts"] is None
    assert scope["runtime_coverage"] is None


@pytest.mark.asyncio
async def test_get_projects_runtime_coverage_opt_in(client) -> None:
    """``?include=runtime_coverage`` computes per-runtime coverage per scope.

    Coverage costs a ``probe_all_runtimes`` pass (per-client config reads) for
    every scope, so — like ``counts`` — it is omitted unless explicitly
    requested. Requesting only coverage must not also trigger counts.
    """
    resp = await client.get("/api/context/projects", params={"include": "runtime_coverage"})
    assert resp.status_code == 200
    scope = resp.json()["scopes"][0]
    assert scope["counts"] is None
    assert isinstance(scope["runtime_coverage"], list)
    runtimes = {r["name"] for r in scope["runtime_coverage"]}
    assert runtimes == {"claude", "gemini", "codex", "kimi"}


@pytest.mark.asyncio
async def test_get_projects_counts_directory_layout_commands_agents(client, cwd_root: Path) -> None:
    """Counts include canonical commands/agents authored in directory layout.

    Regression: ADR-0008 PR-C (#624) changed
    ``list_canonical_{commands,agents}`` to return ``list[tuple[Path,
    Layout]]``, but ``web/routes/context_projects.py:_counts_for``
    still iterated as ``p.stem for p in ...`` — ``p`` was the tuple,
    so ``p.stem`` raised ``AttributeError``. The blanket
    ``except Exception`` hid the failure and silently returned
    ``count=0``, so the UI showed wrong counts without an error
    surface. This test seeds canonical entries in directory layout
    (the new shape that exercises the tuple unpack) and asserts the
    count reflects them.
    """
    # Directory layout: .memtomem/commands/<name>/command.md
    cmd_dir = cwd_root / ".memtomem" / "commands" / "deploy"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "command.md").write_text("# deploy\n", encoding="utf-8")

    # Directory layout: .memtomem/agents/<name>/agent.md
    agent_dir = cwd_root / ".memtomem" / "agents" / "reviewer"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.md").write_text("# reviewer\n", encoding="utf-8")

    resp = await client.get("/api/context/projects", params={"include": "counts"})
    assert resp.status_code == 200
    scope = resp.json()["scopes"][0]
    counts = scope["counts"]
    # With the bug, both would be 0 (blanket except masks AttributeError).
    assert counts["commands"] >= 1, f"commands count should include 'deploy' (got {counts})"
    assert counts["agents"] >= 1, f"agents count should include 'reviewer' (got {counts})"


@pytest.mark.asyncio
async def test_get_projects_after_add(client, tmp_path: Path) -> None:
    other = tmp_path / "inflearn"
    other.mkdir()
    (other / ".claude").mkdir()

    resp = await client.post("/api/context/known-projects", json={"root": str(other)})
    assert resp.status_code == 200, resp.text

    resp = await client.get("/api/context/projects")
    assert resp.status_code == 200
    scopes = resp.json()["scopes"]
    assert len(scopes) == 2
    labels = [s["label"] for s in scopes]
    assert labels[0] == "Server CWD"
    assert labels[1] == "inflearn"


# ── ?target_scope= on /context/projects (#936) ──────────────────────────


@pytest.mark.asyncio
async def test_get_projects_default_target_scope_is_project_shared(client) -> None:
    """Default response echoes ``target_scope = project_shared`` (ADR-0016)."""
    resp = await client.get("/api/context/projects")
    assert resp.status_code == 200
    data = resp.json()
    assert data["target_scope"] == "project_shared"


@pytest.mark.asyncio
async def test_get_projects_project_local_counts_only_with_explicit_target_scope(
    client, cwd_root: Path
) -> None:
    """project_local drafts contribute counts only when explicitly requested.

    Seeds a project_local skill draft and asserts:
      * default response counts it as 0 (project_shared view),
      * ``?target_scope=project_local`` counts it as 1 and echoes the scope.
    """
    local_dir = cwd_root / ".memtomem" / "skills.local" / "draft"
    local_dir.mkdir(parents=True)
    (local_dir / "SKILL.md").write_text("# draft\n", encoding="utf-8")

    default = await client.get("/api/context/projects", params={"include": "counts"})
    assert default.status_code == 200
    assert default.json()["target_scope"] == "project_shared"
    assert default.json()["scopes"][0]["counts"]["skills"] == 0

    explicit = await client.get(
        "/api/context/projects",
        params={"target_scope": "project_local", "include": "counts"},
    )
    assert explicit.status_code == 200
    data = explicit.json()
    assert data["target_scope"] == "project_local"
    assert data["scopes"][0]["counts"]["skills"] == 1


@pytest.mark.asyncio
async def test_get_projects_project_local_dir_layout_commands_agents_count_distinctly(
    client, cwd_root: Path
) -> None:
    """Multiple dir-layout project_local drafts must NOT collapse into one.

    Review P2 on PR #940: ``list_canonical_{commands,agents}`` returns
    ``(Path, Layout)`` where the Path points at the manifest file. For
    directory-layout drafts at ``commands.local/<name>/command.md`` and
    ``agents.local/<name>/agent.md`` the file stem is always
    ``"command"`` / ``"agent"`` respectively, so a naive ``p.stem``
    extractor folded every draft into a single phantom entry. Acute for
    ``target_scope=project_local`` because ``diff_commands`` returns
    nothing (project_local has no runtime fan-out per ADR-0011 §3 /
    ADR-0016 §7) — the canonical count IS the entire signal, so a
    collapse turns two drafts into a count of 1.

    Asserts **exact** counts (not ``>=``) so the test fails pre-fix.
    """
    for name in ("deploy", "ship"):
        cmd_dir = cwd_root / ".memtomem" / "commands.local" / name
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "command.md").write_text(f"# {name}\n", encoding="utf-8")

    for name in ("reviewer", "summarizer"):
        agent_dir = cwd_root / ".memtomem" / "agents.local" / name
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.md").write_text(f"# {name}\n", encoding="utf-8")

    resp = await client.get(
        "/api/context/projects",
        params={"target_scope": "project_local", "include": "counts"},
    )
    assert resp.status_code == 200
    counts = resp.json()["scopes"][0]["counts"]
    assert counts["commands"] == 2, (
        f"two dir-layout project_local commands must count as 2 distinct "
        f"names, not collapse to 1 via p.stem='command' (got {counts})"
    )
    assert counts["agents"] == 2, (
        f"two dir-layout project_local agents must count as 2 distinct "
        f"names, not collapse to 1 via p.stem='agent' (got {counts})"
    )


@pytest.mark.asyncio
async def test_get_projects_project_shared_dir_layout_commands_agents_count_distinctly(
    client, cwd_root: Path
) -> None:
    """Same no-collapse contract for the project_shared (default) tier.

    The bug pre-dates PR #940 — directory-layout drafts under
    ``.memtomem/commands/`` collapsed too — but PR #940 made it more
    visible by exposing a per-tier count. Pinning project_shared here
    keeps the layout-aware helper honest after future refactors.
    """
    for name in ("deploy", "ship"):
        cmd_dir = cwd_root / ".memtomem" / "commands" / name
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "command.md").write_text(f"# {name}\n", encoding="utf-8")

    for name in ("reviewer", "summarizer"):
        agent_dir = cwd_root / ".memtomem" / "agents" / name
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.md").write_text(f"# {name}\n", encoding="utf-8")

    resp = await client.get("/api/context/projects", params={"include": "counts"})
    assert resp.status_code == 200
    counts = resp.json()["scopes"][0]["counts"]
    assert counts["commands"] == 2, counts
    assert counts["agents"] == 2, counts


# ── ?include=counts opt-in + health (ADR-0021 PR2) ──────────────────────


@pytest.mark.asyncio
async def test_get_projects_default_omits_counts(client) -> None:
    """Without ``?include=counts`` every scope reports ``counts: null``."""
    resp = await client.get("/api/context/projects")
    assert resp.status_code == 200
    for scope in resp.json()["scopes"]:
        assert scope["counts"] is None


@pytest.mark.asyncio
async def test_get_projects_include_counts_present(client) -> None:
    """``?include=counts`` restores the full per-type count dict."""
    resp = await client.get("/api/context/projects", params={"include": "counts"})
    assert resp.status_code == 200
    scope = resp.json()["scopes"][0]
    assert scope["counts"] is not None
    assert set(scope["counts"].keys()) == {"skills", "commands", "agents", "mcp-servers"}


@pytest.mark.asyncio
async def test_get_projects_unknown_include_token_ignored(client) -> None:
    """Unrecognized include tokens are ignored (forward-compatible), so a
    stray token neither errors nor accidentally turns counts on."""
    resp = await client.get("/api/context/projects", params={"include": "bogus"})
    assert resp.status_code == 200
    assert resp.json()["scopes"][0]["counts"] is None


@pytest.mark.asyncio
async def test_get_projects_stale_flips_when_memtomem_present(client, cwd_root: Path) -> None:
    """A root without ``.memtomem/`` is stale; creating it clears the flag."""
    # cwd_root has only a .claude marker → stale.
    first = await client.get("/api/context/projects")
    assert first.json()["scopes"][0]["stale"] is True

    (cwd_root / ".memtomem").mkdir()
    second = await client.get("/api/context/projects")
    scope = second.json()["scopes"][0]
    assert scope["stale"] is False
    assert scope["missing"] is False


@pytest.mark.asyncio
async def test_get_projects_missing_is_not_stale(client, tmp_path: Path) -> None:
    """A registered-but-deleted root reports missing=True, stale=False
    (the two health flags are mutually exclusive)."""
    gone = tmp_path / "ghost"
    gone.mkdir()
    add = await client.post("/api/context/known-projects", json={"root": str(gone)})
    sid = add.json()["scope_id"]
    gone.rmdir()

    resp = await client.get("/api/context/projects")
    ghost = next(s for s in resp.json()["scopes"] if s["scope_id"] == sid)
    assert ghost["missing"] is True
    assert ghost["stale"] is False


# ── ?scope_id= on /context/skills ───────────────────────────────────────


@pytest.mark.asyncio
async def test_skills_unknown_scope_id_404(client) -> None:
    resp = await client.get("/api/context/skills?scope_id=p-deadbeefcafe")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_skills_with_scope_id_serves_other_scope(client, tmp_path: Path) -> None:
    """Adding a known project then querying its skills returns that scope's data."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / ".claude").mkdir()
    # Plant a canonical skill in the other scope.
    skill_dir = other / ".memtomem" / "skills" / "from_other"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# from_other\n")

    add_resp = await client.post("/api/context/known-projects", json={"root": str(other)})
    sid = add_resp.json()["scope_id"]

    resp = await client.get(f"/api/context/skills?scope_id={sid}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    names = {s["name"] for s in data["skills"]}
    assert "from_other" in names

    # Without scope_id, the cwd scope must NOT see this skill.
    cwd_resp = await client.get("/api/context/skills")
    cwd_names = {s["name"] for s in cwd_resp.json()["skills"]}
    assert "from_other" not in cwd_names


@pytest.mark.asyncio
async def test_project_scope_id_alias_serves_other_scope(client, tmp_path: Path) -> None:
    other = tmp_path / "canonical-selector"
    other.mkdir()
    (other / ".claude").mkdir()
    skill_dir = other / ".memtomem" / "skills" / "from_project_scope_id"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# from project_scope_id\n", encoding="utf-8")

    add_resp = await client.post("/api/context/known-projects", json={"root": str(other)})
    sid = add_resp.json()["project_scope_id"]
    assert sid == add_resp.json()["scope_id"]

    resp = await client.get("/api/context/skills", params={"project_scope_id": sid})
    assert resp.status_code == 200, resp.text
    names = {s["name"] for s in resp.json()["skills"]}
    assert "from_project_scope_id" in names


@pytest.mark.asyncio
async def test_conflicting_project_scope_aliases_400(client) -> None:
    resp = await client.get(
        "/api/context/skills",
        params={"project_scope_id": "p-111111111111", "scope_id": "p-222222222222"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_overview_with_scope_id_serves_other_scope(client, tmp_path: Path) -> None:
    """The dashboard overview follows the selected project scope."""
    other = tmp_path / "overview-scope"
    other.mkdir()
    (other / ".claude").mkdir()
    skill_dir = other / ".memtomem" / "skills" / "only_other"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# only_other\n", encoding="utf-8")

    add_resp = await client.post("/api/context/known-projects", json={"root": str(other)})
    sid = add_resp.json()["scope_id"]

    scoped = await client.get("/api/context/overview", params={"scope_id": sid})
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["project_root"] == str(other)
    assert scoped.json()["skills"]["total"] == 1

    cwd = await client.get("/api/context/overview")
    assert cwd.status_code == 200, cwd.text
    assert cwd.json()["project_root"] != str(other)
    assert cwd.json()["skills"]["total"] == 0


@pytest.mark.asyncio
async def test_skill_detail_with_scope_id_serves_other_scope(client, tmp_path: Path) -> None:
    """Detail routes use the same selected project as list routes."""
    other = tmp_path / "detail-scope"
    other.mkdir()
    (other / ".claude").mkdir()
    skill_dir = other / ".memtomem" / "skills" / "remote_detail"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# remote detail\n", encoding="utf-8")

    add_resp = await client.post("/api/context/known-projects", json={"root": str(other)})
    sid = add_resp.json()["scope_id"]

    scoped = await client.get("/api/context/skills/remote_detail", params={"scope_id": sid})
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["name"] == "remote_detail"

    cwd = await client.get("/api/context/skills/remote_detail")
    assert cwd.status_code == 404


@pytest.mark.asyncio
async def test_skill_create_with_scope_id_writes_other_scope(client, tmp_path: Path) -> None:
    """Mutating routes can target the active registered project scope."""
    other = tmp_path / "write-scope"
    other.mkdir()
    (other / ".claude").mkdir()

    add_resp = await client.post("/api/context/known-projects", json={"root": str(other)})
    sid = add_resp.json()["scope_id"]

    resp = await client.post(
        "/api/context/skills",
        params={"scope_id": sid},
        json={"name": "created_remote", "content": "# created remotely\n"},
    )
    assert resp.status_code == 200, resp.text
    assert (other / ".memtomem" / "skills" / "created_remote" / "SKILL.md").is_file()

    cwd_resp = await client.get("/api/context/skills")
    cwd_names = {s["name"] for s in cwd_resp.json()["skills"]}
    assert "created_remote" not in cwd_names


# ── #1277: detail / diff / rendered / versions follow the selector ──────
#
# ADR-0015 §2a flagged the detail-route family as silently ignoring
# ``?scope_id=`` (cwd-locked). The remediation routes every read through
# ``resolve_scope_root``; these tests pin the contract per route family by
# planting a same-named artifact with different bytes in the cwd scope and
# a registered scope, then asserting each route serves the *selected*
# project's bytes — the original bug returned cwd bytes with a 200, which
# a presence-only check would miss.


_SCOPED_CMD_TEMPLATE = """---
description: {marker}
---
Body for {marker}.
"""

_SCOPED_AGENT_TEMPLATE = """---
name: {name}
description: {marker}
---
# {marker}
"""


def _plant_artifacts(root: Path, name: str, marker: str) -> None:
    """Plant a same-named canonical skill / command / agent carrying *marker*."""
    skill_dir = root / ".memtomem" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"# {marker}\n", encoding="utf-8")
    cmd_dir = root / ".memtomem" / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    (cmd_dir / f"{name}.md").write_text(
        _SCOPED_CMD_TEMPLATE.format(marker=marker), encoding="utf-8"
    )
    agents_dir = root / ".memtomem" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{name}.md").write_text(
        _SCOPED_AGENT_TEMPLATE.format(name=name, marker=marker), encoding="utf-8"
    )


_DETAIL_FAMILY_ROUTES = [
    pytest.param("/api/context/skills/{name}", "content", id="skills-detail"),
    pytest.param("/api/context/skills/{name}/diff", "canonical_content", id="skills-diff"),
    pytest.param("/api/context/commands/{name}", "content", id="commands-detail"),
    pytest.param(
        "/api/context/commands/{name}/rendered", "canonical_content", id="commands-rendered"
    ),
    pytest.param("/api/context/commands/{name}/diff", "canonical_content", id="commands-diff"),
    pytest.param("/api/context/agents/{name}", "content", id="agents-detail"),
    pytest.param("/api/context/agents/{name}/rendered", "canonical_content", id="agents-rendered"),
    pytest.param("/api/context/agents/{name}/diff", "canonical_content", id="agents-diff"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("template", "content_key"), _DETAIL_FAMILY_ROUTES)
async def test_detail_family_scope_id_selects_project_bytes(
    client, cwd_root: Path, tmp_path: Path, template: str, content_key: str
) -> None:
    name = "scoped_artifact"
    _plant_artifacts(cwd_root, name, marker="from-cwd")
    other = tmp_path / "other-1277"
    other.mkdir()
    (other / ".claude").mkdir()
    _plant_artifacts(other, name, marker="from-other")

    add_resp = await client.post("/api/context/known-projects", json={"root": str(other)})
    sid = add_resp.json()["project_scope_id"]

    url = template.format(name=name)

    scoped = await client.get(url, params={"project_scope_id": sid})
    assert scoped.status_code == 200, scoped.text
    scoped_body = scoped.json()[content_key]
    assert "from-other" in scoped_body
    assert "from-cwd" not in scoped_body

    unscoped = await client.get(url)
    assert unscoped.status_code == 200, unscoped.text
    unscoped_body = unscoped.json()[content_key]
    assert "from-cwd" in unscoped_body
    assert "from-other" not in unscoped_body

    unknown = await client.get(url, params={"project_scope_id": "p-deadbeefcafe"})
    assert unknown.status_code == 404
    assert "unknown project_scope_id" in unknown.json()["detail"]["message"]
    assert unknown.json()["detail"]["error_kind"] == "missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact_type", ["commands", "agents"])
async def test_versions_read_scope_id_follows_selected_project(
    client, tmp_path: Path, artifact_type: str
) -> None:
    """The versions read route resolves the artifact inside the selected scope.

    The artifact exists only in the registered scope, so the scoped read
    succeeding while the bare read 404s proves resolution happened there.
    Flat-layout canonicals answer with ``migrate_required`` rather than an
    error, which is the expected read-only shape for a never-versioned
    artifact.
    """
    name = "versioned_remote"
    other = tmp_path / "versions-1277"
    other.mkdir()
    (other / ".claude").mkdir()
    _plant_artifacts(other, name, marker="versions-other")

    add_resp = await client.post("/api/context/known-projects", json={"root": str(other)})
    sid = add_resp.json()["project_scope_id"]

    url = f"/api/context/{artifact_type}/{name}/versions"

    scoped = await client.get(url, params={"project_scope_id": sid})
    assert scoped.status_code == 200, scoped.text
    data = scoped.json()
    assert data["name"] == name
    assert data["migrate_required"] is True

    unscoped = await client.get(url)
    assert unscoped.status_code == 404

    unknown = await client.get(url, params={"project_scope_id": "p-deadbeefcafe"})
    assert unknown.status_code == 404
    assert "unknown project_scope_id" in unknown.json()["detail"]["message"]
    assert unknown.json()["detail"]["error_kind"] == "missing"


# ── POST /context/known-projects validation ─────────────────────────────


@pytest.mark.asyncio
async def test_post_rejects_relative_path(client) -> None:
    resp = await client.post("/api/context/known-projects", json={"root": "rel/path"})
    assert resp.status_code == 400
    # Path scrubbing in app-level handler is fine; just check status.


@pytest.mark.asyncio
async def test_post_rejects_nonexistent_path(client, tmp_path: Path) -> None:
    nope = tmp_path / "does_not_exist"
    resp = await client.post("/api/context/known-projects", json={"root": str(nope)})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_post_rejects_file_not_dir(client, tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("hi")
    resp = await client.post("/api/context/known-projects", json={"root": str(f)})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_post_warns_on_missing_marker(client, tmp_path: Path) -> None:
    """Empty directories register but the response carries a warning field."""
    bare = tmp_path / "bare"
    bare.mkdir()
    resp = await client.post("/api/context/known-projects", json={"root": str(bare)})
    assert resp.status_code == 200
    data = resp.json()
    assert "scope_id" in data
    assert data["project_scope_id"] == data["scope_id"]
    # Both human prose and the machine-readable code (PR1 pattern) must be present.
    assert "warning" in data
    assert ".claude" in data["warning"]
    assert data.get("warning_code") == "no_runtime_marker"


@pytest.mark.asyncio
async def test_post_no_warning_when_marker_present(client, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".memtomem").mkdir()
    resp = await client.post("/api/context/known-projects", json={"root": str(proj)})
    assert resp.status_code == 200
    body = resp.json()
    assert "warning" not in body
    assert "warning_code" not in body


@pytest.mark.asyncio
async def test_post_blank_label_falls_back_to_basename(client, tmp_path: Path) -> None:
    """A whitespace-only POST label is normalized to None at ingest (matching
    PATCH), so a stored blank can't suppress the basename via label precedence."""
    proj = tmp_path / "inflearn"
    proj.mkdir()
    (proj / ".claude").mkdir()
    resp = await client.post(
        "/api/context/known-projects", json={"root": str(proj), "label": "   "}
    )
    assert resp.status_code == 200
    assert resp.json()["label"] is None

    listing = await client.get("/api/context/projects")
    scope = next(s for s in listing.json()["scopes"] if s["scope_id"] == resp.json()["scope_id"])
    assert scope["label"] == "inflearn"


@pytest.mark.asyncio
async def test_post_idempotent(client, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".claude").mkdir()
    r1 = await client.post("/api/context/known-projects", json={"root": str(proj)})
    r2 = await client.post("/api/context/known-projects", json={"root": str(proj)})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["scope_id"] == r2.json()["scope_id"]
    # ``created`` distinguishes the fresh add from the idempotent re-add (#1292)
    # so the Add Project UI can branch "added" vs "already tracked".
    assert r1.json()["created"] is True
    assert r2.json()["created"] is False
    listing = await client.get("/api/context/projects")
    # cwd + the registered one — exactly two.
    assert len(listing.json()["scopes"]) == 2


@pytest.mark.asyncio
async def test_post_echoes_canonical_root(client, tmp_path: Path) -> None:
    """The response ``root`` is the canonicalized path that was persisted
    (#1644): an absolute-but-non-canonical input (``..`` component) must not
    round-trip verbatim into the registry."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".claude").mkdir()
    detour = tmp_path / "detour"
    detour.mkdir()
    non_canonical = detour / ".." / "proj"

    resp = await client.post("/api/context/known-projects", json={"root": str(non_canonical)})
    assert resp.status_code == 200
    assert resp.json()["root"] == str(proj.resolve())


# ── DELETE /context/known-projects/{scope_id} ───────────────────────────


@pytest.mark.asyncio
async def test_delete_round_trip(client, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".claude").mkdir()
    add = await client.post("/api/context/known-projects", json={"root": str(proj)})
    sid = add.json()["scope_id"]

    resp = await client.delete(f"/api/context/known-projects/{sid}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == sid

    listing = await client.get("/api/context/projects")
    assert len(listing.json()["scopes"]) == 1  # only cwd left


@pytest.mark.asyncio
async def test_delete_unknown_scope_id_404(client) -> None:
    resp = await client.delete("/api/context/known-projects/p-deadbeefcafe")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_removes_stale_entry(client, tmp_path: Path) -> None:
    """A registered root that has since been deleted must still be removable."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".claude").mkdir()
    add = await client.post("/api/context/known-projects", json={"root": str(proj)})
    sid = add.json()["scope_id"]
    # ``compute_scope_id`` is path-derived so removing the dir doesn't change the id.
    assert sid == compute_scope_id(proj)

    proj_claude = proj / ".claude"
    proj_claude.rmdir()
    proj.rmdir()

    resp = await client.delete(f"/api/context/known-projects/{sid}")
    assert resp.status_code == 200


# ── PATCH /context/known-projects/{scope_id} (label rename) ──────────────


@pytest.mark.asyncio
async def test_patch_label_round_trip(client, tmp_path: Path) -> None:
    """PATCH sets the label and echoes it back; root/scope_id are unchanged."""
    proj = tmp_path / "inflearn"
    proj.mkdir()
    (proj / ".claude").mkdir()
    add = await client.post("/api/context/known-projects", json={"root": str(proj)})
    sid = add.json()["scope_id"]

    resp = await client.patch(f"/api/context/known-projects/{sid}", json={"label": "Inflearn Prod"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope_id"] == sid
    assert body["project_scope_id"] == sid
    assert body["root"] == str(proj)
    assert body["label"] == "Inflearn Prod"


@pytest.mark.asyncio
async def test_patch_label_reflected_in_discovery(client, tmp_path: Path) -> None:
    """A renamed project shows its custom label in the GET listing, not the
    directory basename."""
    proj = tmp_path / "inflearn"
    proj.mkdir()
    (proj / ".claude").mkdir()
    add = await client.post("/api/context/known-projects", json={"root": str(proj)})
    sid = add.json()["scope_id"]

    await client.patch(f"/api/context/known-projects/{sid}", json={"label": "Renamed"})

    listing = await client.get("/api/context/projects")
    scope = next(s for s in listing.json()["scopes"] if s["scope_id"] == sid)
    assert scope["label"] == "Renamed"


@pytest.mark.asyncio
async def test_patch_blank_label_clears_to_basename(client, tmp_path: Path) -> None:
    """A whitespace-only label clears the custom label → falls back to basename."""
    proj = tmp_path / "inflearn"
    proj.mkdir()
    (proj / ".claude").mkdir()
    add = await client.post("/api/context/known-projects", json={"root": str(proj)})
    sid = add.json()["scope_id"]

    await client.patch(f"/api/context/known-projects/{sid}", json={"label": "Custom"})
    cleared = await client.patch(f"/api/context/known-projects/{sid}", json={"label": "   "})
    assert cleared.status_code == 200
    assert cleared.json()["label"] is None

    listing = await client.get("/api/context/projects")
    scope = next(s for s in listing.json()["scopes"] if s["scope_id"] == sid)
    assert scope["label"] == "inflearn"


@pytest.mark.asyncio
async def test_patch_unknown_scope_id_404(client) -> None:
    resp = await client.patch("/api/context/known-projects/p-deadbeefcafe", json={"label": "x"})
    assert resp.status_code == 404


# ── PATCH /context/known-projects/{scope_id} (enabled / sync enrollment) ──


async def _register(client, root: Path) -> str:
    root.mkdir()
    (root / ".claude").mkdir()
    add = await client.post("/api/context/known-projects", json={"root": str(root)})
    return add.json()["scope_id"]


@pytest.mark.asyncio
async def test_patch_enabled_toggle_round_trip(client, tmp_path: Path) -> None:
    """PATCH {enabled} flips sync enrollment and is reflected in discovery."""
    sid = await _register(client, tmp_path / "proj")

    off = await client.patch(f"/api/context/known-projects/{sid}", json={"enabled": False})
    assert off.status_code == 200, off.text
    assert off.json()["enabled"] is False
    scope = next(
        s
        for s in (await client.get("/api/context/projects")).json()["scopes"]
        if s["scope_id"] == sid
    )
    assert scope["enabled"] is False
    assert scope["sync_eligible"] is False

    on = await client.patch(f"/api/context/known-projects/{sid}", json={"enabled": True})
    assert on.json()["enabled"] is True
    scope = next(
        s
        for s in (await client.get("/api/context/projects")).json()["scopes"]
        if s["scope_id"] == sid
    )
    assert scope["sync_eligible"] is True


@pytest.mark.asyncio
async def test_patch_enabled_only_preserves_label(client, tmp_path: Path) -> None:
    """Regression: an enabled-only PATCH must not wipe the custom label."""
    sid = await _register(client, tmp_path / "proj")
    await client.patch(f"/api/context/known-projects/{sid}", json={"label": "Keep"})

    resp = await client.patch(f"/api/context/known-projects/{sid}", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["label"] == "Keep"
    assert resp.json()["enabled"] is False
    scope = next(
        s
        for s in (await client.get("/api/context/projects")).json()["scopes"]
        if s["scope_id"] == sid
    )
    assert scope["label"] == "Keep"


@pytest.mark.asyncio
async def test_patch_label_only_preserves_enabled(client, tmp_path: Path) -> None:
    """A label-only PATCH must not silently resume a paused project."""
    sid = await _register(client, tmp_path / "proj")
    await client.patch(f"/api/context/known-projects/{sid}", json={"enabled": False})

    resp = await client.patch(f"/api/context/known-projects/{sid}", json={"label": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False  # still paused


@pytest.mark.asyncio
async def test_patch_enabled_null_leaves_enabled_unchanged(client, tmp_path: Path) -> None:
    """``enabled: null`` means 'unchanged' — it never persists None nor resumes."""
    sid = await _register(client, tmp_path / "proj")
    await client.patch(f"/api/context/known-projects/{sid}", json={"enabled": False})

    resp = await client.patch(
        f"/api/context/known-projects/{sid}", json={"enabled": None, "label": "Renamed"}
    )
    assert resp.status_code == 200
    assert resp.json()["label"] == "Renamed"
    assert resp.json()["enabled"] is False


@pytest.mark.asyncio
async def test_patch_no_fields_is_400(client, tmp_path: Path) -> None:
    sid = await _register(client, tmp_path / "proj")
    resp = await client.patch(f"/api/context/known-projects/{sid}", json={})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_patch_enabled_unknown_scope_id_404(client) -> None:
    resp = await client.patch("/api/context/known-projects/p-deadbeefcafe", json={"enabled": False})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_label_and_enabled_together(client, tmp_path: Path) -> None:
    """A combined PATCH applies both fields in a single atomic store write."""
    sid = await _register(client, tmp_path / "proj")

    resp = await client.patch(
        f"/api/context/known-projects/{sid}", json={"label": "Both", "enabled": False}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["label"] == "Both"
    assert resp.json()["enabled"] is False
    scope = next(
        s
        for s in (await client.get("/api/context/projects")).json()["scopes"]
        if s["scope_id"] == sid
    )
    assert scope["label"] == "Both"
    assert scope["enabled"] is False
    assert scope["sync_eligible"] is False


# ── corrupt known_projects.json → 500, never re-baselined (#1247 id 16) ──


CORRUPT_STORE_VARIANTS = [
    pytest.param(b"not valid {json", id="invalid-json"),
    pytest.param(b'{"version": 999, "projects": []}', id="future-version"),
    pytest.param(b'{"version": 1, "projects": {"root": "/old"}}', id="projects-not-list"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", CORRUPT_STORE_VARIANTS)
async def test_post_over_corrupt_store_500_preserves_bytes(
    client, known_projects_path: Path, tmp_path: Path, corrupt: bytes
) -> None:
    """Registering over a corrupt store must 500 and leave the file alone —
    pre-fix, ``add()`` re-baselined the registered-project list to just the
    new entry."""
    await _register(client, tmp_path / "existing")
    known_projects_path.write_bytes(corrupt)

    other = tmp_path / "other"
    other.mkdir()
    (other / ".claude").mkdir()
    resp = await client.post("/api/context/known-projects", json={"root": str(other)})
    assert resp.status_code == 500, resp.text
    assert known_projects_path.read_bytes() == corrupt


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", CORRUPT_STORE_VARIANTS)
async def test_patch_over_corrupt_store_500_preserves_bytes(
    client, known_projects_path: Path, tmp_path: Path, corrupt: bytes
) -> None:
    """All three PATCH shapes 500 over a corrupt store (not a misleading
    404) and never write."""
    sid = await _register(client, tmp_path / "proj")
    known_projects_path.write_bytes(corrupt)

    for payload in ({"label": "x"}, {"enabled": False}, {"label": "x", "enabled": False}):
        resp = await client.patch(f"/api/context/known-projects/{sid}", json=payload)
        assert resp.status_code == 500, f"{payload}: {resp.text}"
    assert known_projects_path.read_bytes() == corrupt


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", CORRUPT_STORE_VARIANTS)
async def test_delete_over_corrupt_store_500_preserves_bytes(
    client, known_projects_path: Path, tmp_path: Path, corrupt: bytes
) -> None:
    """DELETE over a corrupt store is a server error, not "already gone"."""
    sid = await _register(client, tmp_path / "proj")
    known_projects_path.write_bytes(corrupt)

    resp = await client.delete(f"/api/context/known-projects/{sid}")
    assert resp.status_code == 500, resp.text
    assert known_projects_path.read_bytes() == corrupt
