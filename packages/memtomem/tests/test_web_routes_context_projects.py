"""HTTP-layer tests for the multi-project context-gateway routes (PR2).

Covers:
- ``GET /api/context/projects`` shape with cwd-only and cwd+known scopes.
- ``?scope_id=`` query on ``/api/context/skills`` (and unknown scope_id 404).
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


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def cwd_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox HOME; return the cwd project root with a .claude marker."""
    monkeypatch.setenv("HOME", str(tmp_path))
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
    assert scope["label"] == "Server CWD"
    assert scope["sources"] == ["server-cwd"]
    assert scope["tier"] == "project"
    assert scope["missing"] is False
    assert scope["experimental"] is False
    assert "counts" in scope
    assert set(scope["counts"].keys()) == {"skills", "commands", "agents"}


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
async def test_post_idempotent(client, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".claude").mkdir()
    r1 = await client.post("/api/context/known-projects", json={"root": str(proj)})
    r2 = await client.post("/api/context/known-projects", json={"root": str(proj)})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["scope_id"] == r2.json()["scope_id"]
    listing = await client.get("/api/context/projects")
    # cwd + the registered one — exactly two.
    assert len(listing.json()["scopes"]) == 2


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
