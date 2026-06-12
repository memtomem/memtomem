"""Backend sync-eligibility write-guard tests (#1203 §1i).

``resolve_writable_scope_root`` refuses the runtime-writing endpoints
(``/context/{agents,commands,skills,mcp-servers}/sync``, ``/settings-sync`` and
its conflict mutators ``resolve`` / ``rules/delete``) for a project scope that is
not sync-eligible — an enrolled-then-paused known project, or a discovery-only
``claude-projects`` scan row that was never enrolled. The Web UI and
``mm context update --all`` already skip these projects; this is the
defense-in-depth backend gate so that a *direct* API call cannot push into a
paused project's runtime.

Canonical-management endpoints (create / update / delete / import, and rule
``promote``) write the canonical source — not the project's runtime — so they
stay ungated: pausing sync must not block preparing a project's artifacts.

server-cwd is always eligible (the running directory can't be paused), even
when it has also been enrolled and then paused (the trusted ``server-cwd``
source coalesces and keeps ``sync_eligible`` true).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import ContextGatewayConfig, Mem2MemConfig
from memtomem.context import projects as proj_mod
from memtomem.web.app import create_app

from .helpers import set_home


# ── Fixtures (mirror test_web_routes_context_projects.py) ────────────────


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
    application = create_app(lifespan=None, mode="dev")
    application.state.project_root = cwd_root
    application.state.storage = AsyncMock()
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


# ── Helpers ──────────────────────────────────────────────────────────────


async def _enroll(client, root: Path) -> str:
    """Register *root* as a known project; return its scope_id."""
    resp = await client.post("/api/context/known-projects", json={"root": str(root)})
    assert resp.status_code == 200, resp.text
    return resp.json()["project_scope_id"]


async def _pause(client, scope_id: str) -> None:
    """Pause sync enrollment for *scope_id* (PATCH enabled=False)."""
    resp = await client.patch(f"/api/context/known-projects/{scope_id}", json={"enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False


async def _post(client, path: str, *, params: dict, body: dict | None = None):
    kwargs = {"json": body} if body is not None else {}
    return await client.post(path, params=params, **kwargs)


def _guard_reason(resp) -> str | None:
    """Return the write-guard ``reason_code`` if *resp* is our 409, else None.

    Distinguishes the sync-eligibility 409 (structured ``detail`` dict) from
    the plain-string 409s the same endpoints raise for other reasons
    (``create`` "already exists", ``update`` mtime conflict).
    """
    if resp.status_code != 409:
        return None
    detail = resp.json().get("detail")
    if isinstance(detail, dict):
        return detail.get("reason_code")
    return None


# The seven runtime-writing mutators the guard protects: (label, path, body).
# ``None`` body → endpoint with an optional request body (sent with no JSON).
GATED_ENDPOINTS = [
    ("agents_sync", "/api/context/agents/sync", None),
    ("commands_sync", "/api/context/commands/sync", None),
    ("skills_sync", "/api/context/skills/sync", None),
    ("mcp_servers_sync", "/api/context/mcp-servers/sync", None),
    ("settings_apply", "/api/settings-sync", None),
    ("settings_resolve", "/api/settings-sync/resolve", {"event": "PreToolUse", "matcher": ""}),
    (
        "settings_delete",
        "/api/settings-sync/rules/delete",
        {"event": "PreToolUse", "rule_index": 0, "rule_hash": "deadbeef"},
    ),
]
_GATED_IDS = [e[0] for e in GATED_ENDPOINTS]

# The artifact sync endpoints that return 200 on a clean enrolled project.
SYNC_ENDPOINTS = [e for e in GATED_ENDPOINTS if e[0].endswith("_sync")]
_SYNC_IDS = [e[0] for e in SYNC_ENDPOINTS]


# ── Paused → 409 across every gated mutator ──────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("label,path,body", GATED_ENDPOINTS, ids=_GATED_IDS)
async def test_gated_mutator_rejects_paused_project(
    client, tmp_path: Path, label: str, path: str, body: dict | None
) -> None:
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    sid = await _enroll(client, proj)
    await _pause(client, sid)

    resp = await _post(client, path, params={"project_scope_id": sid}, body=body)

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "sync_paused"
    assert detail["project_scope_id"] == sid


# ── Eligible scopes pass the guard ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("label,path,body", SYNC_ENDPOINTS, ids=_SYNC_IDS)
async def test_gated_sync_allows_enrolled_enabled(
    client, tmp_path: Path, label: str, path: str, body: dict | None
) -> None:
    """An enrolled, enabled project is sync-eligible — the guard lets it through."""
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    sid = await _enroll(client, proj)  # enabled defaults True

    resp = await _post(client, path, params={"project_scope_id": sid}, body=body)

    assert resp.status_code == 200, resp.text
    assert _guard_reason(resp) is None


@pytest.mark.asyncio
async def test_no_selector_server_cwd_passes(client) -> None:
    """No selector → server cwd, which is always sync-eligible."""
    resp = await client.post("/api/context/agents/sync")
    assert resp.status_code == 200, resp.text
    assert _guard_reason(resp) is None


@pytest.mark.asyncio
async def test_paused_cwd_still_eligible_via_server_cwd(client, cwd_root: Path) -> None:
    """Enrolling + pausing the *running* directory must not lock it out.

    The trusted ``server-cwd`` source coalesces with the paused known-projects
    entry, so ``sync_eligible`` stays true — you can't pause the dir you're in.
    """
    sid = await _enroll(client, cwd_root)
    await _pause(client, sid)

    resp = await client.post("/api/context/agents/sync", params={"project_scope_id": sid})

    assert resp.status_code == 200, resp.text
    assert _guard_reason(resp) is None


@pytest.mark.asyncio
async def test_scan_only_never_enrolled_rejected(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discovery-only claude-projects scan row was never enrolled → 409.

    Distinct ``reason_code`` from the paused case so the client can render
    "enroll first" vs "resume" (mirrors the Web UI tooltip split).
    """
    cp = tmp_path / "fake_home" / ".claude" / "projects"
    cp.mkdir(parents=True)
    # Overrides the suite-wide ``_isolate_claude_projects_scan`` autouse pin.
    monkeypatch.setattr(proj_mod, "_CLAUDE_PROJECTS_DIR", cp)
    scan_root = tmp_path / "scanned_proj"
    (scan_root / ".claude").mkdir(parents=True)
    (cp / proj_mod._encode_claude_project_path(scan_root)).mkdir()

    listing = await client.get("/api/context/projects")
    scopes = listing.json()["scopes"]
    scan = next(
        s for s in scopes if "claude-projects" in s["sources"] and "server-cwd" not in s["sources"]
    )
    assert scan["sync_eligible"] is False

    resp = await client.post(
        "/api/context/agents/sync", params={"project_scope_id": scan["project_scope_id"]}
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason_code"] == "sync_not_enrolled"


# ── Both project-runtime tiers gated; user tier exempt ───────────────────

# The settings-sync routes legitimately accept ``project_local`` (they have NO
# ``_reject_project_local_write`` tier backstop), so the guard — not the route —
# is the only thing standing between a paused project and a ``project_local``
# runtime write.
_SETTINGS_RUNTIME_WRITES = [e for e in GATED_ENDPOINTS if e[0].startswith("settings_")]
_SETTINGS_IDS = [e[0] for e in _SETTINGS_RUNTIME_WRITES]


@pytest.mark.asyncio
@pytest.mark.parametrize("label,path,body", _SETTINGS_RUNTIME_WRITES, ids=_SETTINGS_IDS)
async def test_settings_project_local_on_paused_rejected(
    client, tmp_path: Path, label: str, path: str, body: dict | None
) -> None:
    """Regression (review blocker): ``project_local`` is a project-runtime write
    (``<root>/.claude/settings.local.json``). The settings routes have no
    ``_reject_project_local_write`` backstop, so the guard must 409 ``project_local``
    on a paused project — and nothing may be written under ``proj/.claude/``.
    """
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    sid = await _enroll(client, proj)
    await _pause(client, sid)

    resp = await _post(
        client, path, params={"project_scope_id": sid, "target_scope": "project_local"}, body=body
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason_code"] == "sync_paused"
    assert not (proj / ".claude" / "settings.local.json").exists()


@pytest.mark.asyncio
async def test_artifact_sync_project_local_on_paused_rejected_by_guard(
    client, tmp_path: Path
) -> None:
    """For artifact sync, ``project_local`` + paused now 409s via the eligibility
    guard (gated for every project tier). The guard runs before the route's
    ``project_shared``-only tier gate, so the response is the structured 409,
    not a plain 400.
    """
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    sid = await _enroll(client, proj)
    await _pause(client, sid)

    resp = await client.post(
        "/api/context/agents/sync",
        params={"project_scope_id": sid, "target_scope": "project_local"},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason_code"] == "sync_paused"


@pytest.mark.asyncio
async def test_user_tier_exempt_on_paused(client, tmp_path: Path) -> None:
    """``user`` targets global ``~/.claude``, not the project runtime, so a
    paused project does not block it — the guard exempts user-tier writes.
    """
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    sid = await _enroll(client, proj)
    await _pause(client, sid)

    resp = await _post(
        client, "/api/settings-sync", params={"project_scope_id": sid, "target_scope": "user"}
    )

    assert _guard_reason(resp) is None


@pytest.mark.asyncio
async def test_settings_project_local_on_enabled_allowed(client, tmp_path: Path) -> None:
    """A legitimate ``project_local`` settings sync to an enrolled+enabled project
    must NOT be blocked — the guard only refuses sync-ineligible scopes.
    """
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    sid = await _enroll(client, proj)  # enabled defaults True

    resp = await _post(
        client,
        "/api/settings-sync",
        params={"project_scope_id": sid, "target_scope": "project_local"},
    )

    assert _guard_reason(resp) is None


# ── Canonical-management endpoints stay ungated on a paused project ───────


@pytest.mark.asyncio
async def test_canonical_create_not_gated_on_paused(client, tmp_path: Path) -> None:
    """Creating a canonical agent writes the source, not the runtime → allowed."""
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    sid = await _enroll(client, proj)
    await _pause(client, sid)

    resp = await client.post(
        "/api/context/agents",
        params={"project_scope_id": sid},
        json={"name": "helper", "content": "# helper\n"},
    )

    assert resp.status_code == 200, resp.text
    assert _guard_reason(resp) is None


@pytest.mark.asyncio
async def test_import_not_gated_on_paused(client, tmp_path: Path) -> None:
    """Import pulls runtime → canonical (a canonical write) → not pause-gated."""
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    sid = await _enroll(client, proj)
    await _pause(client, sid)

    resp = await client.post("/api/context/agents/import", params={"project_scope_id": sid})

    assert resp.status_code == 200, resp.text
    assert _guard_reason(resp) is None


@pytest.mark.asyncio
async def test_promote_rule_not_gated_on_paused(client, tmp_path: Path) -> None:
    """``rules/promote`` writes the canonical settings file → not pause-gated.

    With no target rule present it fails downstream (not our 409); the point is
    the write-guard never fires for this canonical-direction endpoint.
    """
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    sid = await _enroll(client, proj)
    await _pause(client, sid)

    resp = await client.post(
        "/api/settings-sync/rules/promote",
        params={"project_scope_id": sid},
        json={"event": "PreToolUse", "rule_index": 0, "rule_hash": "deadbeef"},
    )

    assert _guard_reason(resp) is None


# ── Cascade delete: canonical ungated, runtime-cascade gated ──────────────
#
# DELETE /context/{type}/{name} removes the canonical source (ungated), but
# ``?cascade=true`` ALSO unlinks the generated runtime copies — a runtime
# write — so only the cascade variant is sync-eligibility-gated, via
# ``resolve_scope_root_cascade_gated``. (Codex review catch.)

_CASCADE_KINDS = ["agents", "commands", "skills"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _CASCADE_KINDS)
async def test_cascade_delete_on_paused_rejected(client, tmp_path: Path, kind: str) -> None:
    """``cascade=true`` delete unlinks runtime copies → gated 409 on a paused project."""
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    sid = await _enroll(client, proj)
    await _pause(client, sid)

    resp = await client.delete(
        f"/api/context/{kind}/anyname",
        params={"project_scope_id": sid, "cascade": "true"},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason_code"] == "sync_paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _CASCADE_KINDS)
async def test_plain_delete_on_paused_not_gated(client, tmp_path: Path, kind: str) -> None:
    """A plain (non-cascade) delete touches only the canonical source → ungated."""
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    sid = await _enroll(client, proj)
    await _pause(client, sid)

    # cascade defaults False → no runtime write → the guard must not fire.
    resp = await client.delete(f"/api/context/{kind}/nonexistent", params={"project_scope_id": sid})

    assert _guard_reason(resp) is None


@pytest.mark.asyncio
async def test_mcp_cascade_delete_on_paused_not_gated(client, tmp_path: Path) -> None:
    """MCP cascade delete is a documented v1 no-op (no runtime unlink), so it is
    intentionally NOT gated — gating a no-op would 409 a harmless request.
    """
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    sid = await _enroll(client, proj)
    await _pause(client, sid)

    resp = await client.delete(
        "/api/context/mcp-servers/nonexistent",
        params={"project_scope_id": sid, "cascade": "true"},
    )

    assert _guard_reason(resp) is None
