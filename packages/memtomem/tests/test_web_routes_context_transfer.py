"""HTTP-layer tests for ``POST /api/context/{kind}/{name}/transfer`` (A-5 #1276).

The engine matrix (cross-project move/copy, Gate A residue, rollback,
provenance carry-over) is pinned by ``test_context_transfer.py``; this file
covers what the WEB surface adds (ADR-0023 §10):

- destination resolution through project discovery (404 unknown,
  409 ``sync_paused`` incl. the implicit same-project destination,
  409 ``no_memtomem_store``);
- the disclose-then-confirm round-trips (Gate B ``confirm_project_shared``,
  user-tier ``allow_host_writes`` with ``host_targets``);
- the object error envelope (``error_kind`` + ``reason_code``) and the
  issue-pinned string 422 for ``PrivacyScanError``;
- ``dry_run`` plan responses and the provenance triple on the wire.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import ContextGatewayConfig, Mem2MemConfig
from memtomem.context.lockfile import Lockfile, utcnow_iso8601_z
from memtomem.web.app import create_app
from .helpers import set_home

_AGENT_BODY = "---\nname: foo\ndescription: a clean test agent\n---\n\nhello\n"
_SECRET_BODY = "---\nname: foo\ndescription: leaky\n---\n\nkey=AKIA1234567890ABCDEF\n"


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def cwd_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox HOME; return the cwd project root with markers + store."""
    set_home(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".claude").mkdir()
    (cwd / ".memtomem").mkdir()
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


def _write_agent(store_root: Path, name: str, body: str = _AGENT_BODY) -> Path:
    """Dir-layout canonical agent under *store_root* (an ``agents`` dir)."""
    artifact = store_root / name
    artifact.mkdir(parents=True, exist_ok=True)
    manifest = artifact / "agent.md"
    manifest.write_text(body, encoding="utf-8")
    return manifest


def _shared_agents(root: Path) -> Path:
    return root / ".memtomem" / "agents"


def _local_agents(root: Path) -> Path:
    return root / ".memtomem" / "agents.local"


async def _register(client, root: Path, *, enabled: bool | None = None) -> str:
    """Register *root* as a known project; optionally flip enrollment."""
    resp = await client.post("/api/context/known-projects", json={"root": str(root)})
    assert resp.status_code == 200, resp.text
    scope_id = resp.json()["project_scope_id"]
    if enabled is not None:
        resp = await client.patch(
            f"/api/context/known-projects/{scope_id}", json={"enabled": enabled}
        )
        assert resp.status_code == 200, resp.text
    return scope_id


def _other_project(tmp_path: Path, name: str = "proj-b", *, store: bool = True) -> Path:
    other = tmp_path / name
    other.mkdir()
    (other / ".claude").mkdir()
    if store:
        (other / ".memtomem").mkdir()
    return other


# ── apply: cross-project move / copy ────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_project_move_ok(client, cwd_root: Path, tmp_path: Path) -> None:
    """shared→shared move A→B: 200 ok + result fields the UI builds on."""
    src_manifest = _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "to_target_scope": "project_shared",
            "to_project_scope_id": scope_b,
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["transferred"] is True
    assert data["mode"] == "move"
    assert data["from_scope"] == "project_shared"
    assert data["to_scope"] == "project_shared"
    # One-click follow-up sync contract: the destination scope_id + the
    # engine's exact cd-prefixed command.
    assert data["dst_project_scope_id"] == scope_b
    assert data["needs_sync"] is True
    assert data["sync_command"].startswith("cd ")
    assert "mm context sync --scope project_shared" in data["sync_command"]
    # Provenance triple is on the wire even in the quiet case.
    assert data["provenance"] == "not_applicable"
    assert data["provenance_reason"] is None
    assert data["provenance_reason_code"] is None

    assert not src_manifest.parent.exists()
    dst_manifest = _shared_agents(other) / "foo" / "agent.md"
    assert dst_manifest.read_text(encoding="utf-8") == _AGENT_BODY


@pytest.mark.asyncio
async def test_cross_project_copy_keeps_source(client, cwd_root: Path, tmp_path: Path) -> None:
    src_manifest = _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "copy",
            "to_target_scope": "project_shared",
            "to_project_scope_id": scope_b,
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    assert src_manifest.read_text(encoding="utf-8") == _AGENT_BODY
    assert (_shared_agents(other) / "foo" / "agent.md").is_file()


@pytest.mark.asyncio
async def test_copy_rename_surfaces_provenance_reason_code(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """A renamed shared→shared copy of a wiki-tracked source never carries
    provenance — the response must surface the typed reason code (the A-4
    ``_skip_reasons`` contract this surface exists to expose)."""
    _write_agent(_shared_agents(cwd_root), "foo")
    Lockfile.at(cwd_root).upsert_entry(
        "agents", "foo", wiki_commit="abc123", installed_at=utcnow_iso8601_z()
    )
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "copy",
            "to_target_scope": "project_shared",
            "to_project_scope_id": scope_b,
            "as_name": "bar",
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["dst_name"] == "bar"
    assert data["provenance"] == "not_carried"
    assert data["provenance_reason_code"] == "renamed_copy"
    assert "lock.json entries are keyed by wiki asset name" in data["provenance_reason"]
    assert (_shared_agents(other) / "bar" / "agent.md").is_file()


# ── destination refusals ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collision_409_destination_exists(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    _write_agent(_shared_agents(other), "foo", "---\nname: foo\n---\n\nresident\n")
    scope_b = await _register(client, other)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "to_target_scope": "project_shared",
            "to_project_scope_id": scope_b,
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "conflict"
    assert detail["reason_code"] == "destination_exists"
    assert "destination already exists" in detail["message"]


@pytest.mark.asyncio
async def test_paused_destination_409_sync_paused(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other, enabled=False)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "to_target_scope": "project_shared",
            "to_project_scope_id": scope_b,
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "conflict"
    assert detail["reason_code"] == "sync_paused"
    assert detail["project_scope_id"] == scope_b
    # Nothing moved.
    assert (_shared_agents(cwd_root) / "foo" / "agent.md").is_file()


@pytest.mark.asyncio
async def test_implicit_destination_in_paused_source_project_409(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """Codex design-gate Major-2 pin: omitting ``to_project_scope_id`` must
    not write a project tier the explicit spelling of the same destination
    refuses. Source selector names a paused project; destination implicit."""
    other = _other_project(tmp_path)
    _write_agent(_shared_agents(other), "foo")
    scope_b = await _register(client, other, enabled=False)

    resp = await client.post(
        f"/api/context/agents/foo/transfer?project_scope_id={scope_b}",
        json={"mode": "move", "to_target_scope": "project_local"},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "sync_paused"
    assert (_shared_agents(other) / "foo" / "agent.md").is_file()


@pytest.mark.asyncio
async def test_user_tier_from_paused_source_project_not_gated_by_eligibility(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """User-tier destination is a host write, not the paused project's
    runtime — eligibility must not refuse it (allow_host_writes gates it)."""
    other = _other_project(tmp_path)
    _write_agent(_shared_agents(other), "foo")
    scope_b = await _register(client, other, enabled=False)

    resp = await client.post(
        f"/api/context/agents/foo/transfer?project_scope_id={scope_b}",
        json={"mode": "move", "to_target_scope": "user", "allow_host_writes": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    assert (Path.home() / ".memtomem" / "agents" / "foo" / "agent.md").is_file()


@pytest.mark.asyncio
async def test_unknown_destination_scope_404(client, cwd_root: Path) -> None:
    _write_agent(_shared_agents(cwd_root), "foo")
    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "to_target_scope": "project_shared",
            "to_project_scope_id": "p-deadbeef0000",
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "missing"
    assert "unknown project_scope_id" in detail["message"]


@pytest.mark.asyncio
async def test_destination_without_store_409(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_agent(_shared_agents(cwd_root), "foo")
    bare = _other_project(tmp_path, "bare", store=False)
    scope_b = await _register(client, bare)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "copy",
            "to_target_scope": "project_shared",
            "to_project_scope_id": scope_b,
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "no_memtomem_store"
    assert "mm context init" in detail["message"]


@pytest.mark.asyncio
async def test_source_not_found_404(client, cwd_root: Path) -> None:
    resp = await client.post(
        "/api/context/agents/ghost/transfer",
        json={"mode": "move", "to_target_scope": "project_local"},
    )
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "missing"
    assert "not found in any scope" in detail["message"]


# ── disclose-then-confirm round-trips ────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_b_round_trip(client, cwd_root: Path) -> None:
    """project_shared destination: disclose (no write) → confirmed re-POST."""
    src_manifest = _write_agent(_local_agents(cwd_root), "foo")

    first = await client.post(
        "/api/context/agents/foo/transfer",
        json={"mode": "move", "to_target_scope": "project_shared"},
    )
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["status"] == "needs_confirmation"
    assert data["confirm"] == "confirm_project_shared"
    assert "host_targets" not in data
    plan = data["plan"]
    assert plan["transferred"] is False
    assert plan["dst_path"].endswith("foo")
    # Disclosure performed no write.
    assert src_manifest.is_file()
    assert not (_shared_agents(cwd_root) / "foo").exists()

    second = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "to_target_scope": "project_shared",
            "confirm_project_shared": True,
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "ok"
    assert (_shared_agents(cwd_root) / "foo" / "agent.md").is_file()
    assert not src_manifest.parent.exists()


@pytest.mark.asyncio
async def test_host_write_round_trip(client, cwd_root: Path) -> None:
    """user-tier destination: host paths disclosed → allow_host_writes re-POST."""
    src_manifest = _write_agent(_shared_agents(cwd_root), "foo")
    host_dst = Path.home() / ".memtomem" / "agents" / "foo"

    first = await client.post(
        "/api/context/agents/foo/transfer",
        json={"mode": "move", "to_target_scope": "user"},
    )
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["status"] == "needs_confirmation"
    assert data["confirm"] == "allow_host_writes"
    assert data["host_targets"] == [str(host_dst)]
    assert data["plan"]["dst_project_scope_id"] is None
    assert src_manifest.is_file()
    assert not host_dst.exists()

    second = await client.post(
        "/api/context/agents/foo/transfer",
        json={"mode": "move", "to_target_scope": "user", "allow_host_writes": True},
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "ok"
    assert (host_dst / "agent.md").read_text(encoding="utf-8") == _AGENT_BODY
    assert not src_manifest.parent.exists()


@pytest.mark.asyncio
async def test_dry_run_plans_without_confirmation(client, cwd_root: Path) -> None:
    src_manifest = _write_agent(_local_agents(cwd_root), "foo")
    resp = await client.post(
        "/api/context/agents/foo/transfer?dry_run=true",
        json={"mode": "move", "to_target_scope": "project_shared"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "plan"
    assert data["transferred"] is False
    assert src_manifest.is_file()
    assert not (_shared_agents(cwd_root) / "foo").exists()


# ── Gate A / privacy envelope ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_privacy_scan_blocks_with_standard_string_envelope(client, cwd_root: Path) -> None:
    """Secret bytes landing in project_shared → the standard 422 STRING
    envelope (issue-pinned), zero residue at the destination. Copy mode
    carries the transfer-native source-anchored remediation hint (a
    same-root move keeps migrate's historical Gate A wording — engine
    contract, pinned in test_context_transfer.py)."""
    src_manifest = _write_agent(_local_agents(cwd_root), "foo", _SECRET_BODY)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "copy",
            "to_target_scope": "project_shared",
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert "Offending file" in detail
    # The hint re-anchors onto the SOURCE manifest (the transient staging
    # path is gone after rollback).
    assert str(src_manifest) in detail
    # Zero residue at the destination; copy never touched the source.
    assert not (_shared_agents(cwd_root) / "foo").exists()
    assert src_manifest.read_text(encoding="utf-8") == _SECRET_BODY


# ── validation 400s ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_destination_with_project_selector_400(client, cwd_root: Path) -> None:
    _write_agent(_shared_agents(cwd_root), "foo")
    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "to_target_scope": "user",
            "to_project_scope_id": "p-cafecafe0000",
            "allow_host_writes": True,
        },
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "validation"
    assert "user tier is global" in detail["message"]


@pytest.mark.asyncio
async def test_rename_with_move_400(client, cwd_root: Path) -> None:
    _write_agent(_shared_agents(cwd_root), "foo")
    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "to_target_scope": "project_local",
            "as_name": "bar",
        },
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "validation"
    assert "copy mode only" in detail["message"]


@pytest.mark.asyncio
async def test_invalid_name_400(client, cwd_root: Path) -> None:
    """Name validation fires before any path math (CLI-parity). A
    slash-bearing traversal shape can't even reach the handler — the
    router 404s a path-param slash — so the reachable invalid class is
    charset violations."""
    resp = await client.post(
        "/api/context/agents/foo$/transfer",
        json={"mode": "move", "to_target_scope": "project_local"},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "validation"
    assert "invalid agent name" in detail["message"]


@pytest.mark.asyncio
async def test_unsupported_kind_400(client, cwd_root: Path) -> None:
    # "mcp-servers" graduated to a supported kind in A-12 (#1282) — the
    # unsupported example is now a kind no surface accepts.
    resp = await client.post(
        "/api/context/memories/foo/transfer",
        json={"mode": "move", "to_target_scope": "project_local"},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "validation"
    assert "unsupported kind for artifact transfer" in detail["message"]
    assert "'mcp-servers'" in detail["message"]  # the 400 advertises the new kind


# ── timeout envelope ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_503_busy_with_bounded_lock_budget(
    client, cwd_root: Path, monkeypatch
) -> None:
    """Engine lock-budget TimeoutError → 503 busy. The fake also records the
    call kwargs: the route MUST pass the bounded ``lock_timeout`` — an
    unbounded (None) engine wait would silently reopen the #1145
    orphan-worker shape while this test kept passing (Codex review)."""
    import memtomem.web.routes.context_transfer as route_mod

    _write_agent(_shared_agents(cwd_root), "foo")
    seen_kwargs: dict = {}

    def hung_transfer(*args, **kwargs):
        seen_kwargs.update(kwargs)
        raise TimeoutError("could not acquire .lock within 30s (held by another process)")

    monkeypatch.setattr(route_mod, "transfer_artifact", hung_transfer)
    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={"mode": "move", "to_target_scope": "project_local"},
    )
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "busy"
    assert "Transfer timed out" in detail["message"]
    assert seen_kwargs["lock_timeout"] == route_mod._TRANSFER_LOCK_BUDGET_S


# ── mcp-servers copy (A-12 #1282) ────────────────────────────────────
#
# Adapter semantics are pinned by ``test_mcp_servers_copy.py``; these pin
# the route's mcp branch: the validation 400 matrix in mcp vocabulary,
# the shared gates (confirm round-trip, eligibility, store check)
# applying unchanged, the issue-pinned 422s (string privacy envelope /
# object parse envelope), and the wire shape (``sync_hint`` prose with
# ``sync_command`` null — no CLI sync phase exists for mcp-servers).


_MCP_CLEAN = {"command": "npx", "args": ["-y", "srv"], "env": {"PG_HOST": "localhost"}}
_MCP_SECRET = {"command": "npx", "env": {"AWS_ACCESS_KEY": "AKIA1234567890ABCDEF"}}


def _write_mcp_server(root: Path, name: str = "pg", definition: dict | None = None) -> Path:
    store = root / ".memtomem" / "mcp-servers"
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{name}.json"
    path.write_text(
        json.dumps(definition if definition is not None else _MCP_CLEAN, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _mcp_copy_body(scope_b: str, **overrides) -> dict:
    body = {
        "mode": "copy",
        "to_target_scope": "project_shared",
        "to_project_scope_id": scope_b,
        "confirm_project_shared": True,
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_mcp_copy_ok(client, cwd_root: Path, tmp_path: Path) -> None:
    src = _write_mcp_server(cwd_root)
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["transferred"] is True
    assert data["kind"] == "mcp-servers"
    assert data["mode"] == "copy"
    assert data["layout"] == "flat"
    assert (data["from_scope"], data["to_scope"]) == ("project_shared", "project_shared")
    assert data["dst_project_scope_id"] == scope_b
    # Follow-up contract: needs_sync with prose (no runnable CLI command).
    assert data["needs_sync"] is True
    assert data["sync_command"] is None
    assert f"project_scope_id={scope_b}" in data["sync_hint"]
    assert data["provenance"] == "not_applicable"

    dst = other / ".memtomem" / "mcp-servers" / "pg.json"
    assert dst.read_bytes() == src.read_bytes()
    assert src.is_file()


@pytest.mark.asyncio
async def test_mcp_needs_confirmation_round_trip(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_mcp_server(cwd_root)
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)
    dst = other / ".memtomem" / "mcp-servers" / "pg.json"

    first = await client.post(
        "/api/context/mcp-servers/pg/transfer",
        json=_mcp_copy_body(scope_b, confirm_project_shared=False),
    )
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["status"] == "needs_confirmation"
    assert data["confirm"] == "confirm_project_shared"
    assert data["plan"]["transferred"] is False
    assert data["plan"]["sync_command"] is None
    assert data["plan"]["sync_hint"]
    assert not dst.exists()

    second = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "ok"
    assert dst.is_file()


@pytest.mark.asyncio
async def test_mcp_dry_run_plan_writes_nothing(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_mcp_server(cwd_root)
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post(
        "/api/context/mcp-servers/pg/transfer?dry_run=true",
        json=_mcp_copy_body(scope_b, confirm_project_shared=False),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "plan"
    assert data["transferred"] is False
    assert not (other / ".memtomem" / "mcp-servers").exists()


@pytest.mark.asyncio
async def test_mcp_validation_400_matrix(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_mcp_server(cwd_root)
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    cases = [
        (_mcp_copy_body(scope_b, mode="move"), "mcp-servers support copy only"),
        (_mcp_copy_body(scope_b, as_name="pg2"), "as_name is not supported for mcp-servers"),
        (
            _mcp_copy_body(scope_b, to_target_scope="project_local"),
            "single-tier (project_shared) by design",
        ),
        (
            _mcp_copy_body(scope_b, from_scope="user"),
            "single-tier (project_shared) by design",
        ),
        (
            _mcp_copy_body(scope_b, to_project_scope_id=None),
            "mcp-servers copy is cross-project only",
        ),
    ]
    for body, fragment in cases:
        resp = await client.post("/api/context/mcp-servers/pg/transfer", json=body)
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["error_kind"] == "validation"
        assert fragment in detail["message"]
    assert not (other / ".memtomem" / "mcp-servers").exists()


@pytest.mark.asyncio
async def test_mcp_same_project_selector_400(client, cwd_root: Path) -> None:
    _write_mcp_server(cwd_root)
    scope_self = await _register(client, cwd_root)

    resp = await client.post(
        "/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_self)
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "validation"
    assert "resolves to the source project" in detail["message"]


@pytest.mark.asyncio
async def test_mcp_collision_409(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_mcp_server(cwd_root)
    other = _other_project(tmp_path)
    existing = _write_mcp_server(other, definition={"command": "theirs"})
    before = existing.read_bytes()
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "conflict"
    assert detail["reason_code"] == "destination_exists"
    assert existing.read_bytes() == before


@pytest.mark.asyncio
async def test_mcp_privacy_422_string_envelope(client, cwd_root: Path, tmp_path: Path) -> None:
    """Issue #1282 acceptance 1 on the wire: the standard project_shared
    block envelope — a STRING 422 detail, same as every sync surface."""
    _write_mcp_server(cwd_root, definition=_MCP_SECRET)
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert detail.startswith("Gate A: pg.json contains 1 privacy pattern hit(s)")
    # Zero destination residue: no canonical, no staging leftovers (the
    # store dir + lock sidecar are the locking machinery's own artifacts).
    dst_store = other / ".memtomem" / "mcp-servers"
    assert not (dst_store / "pg.json").exists()
    assert not list(dst_store.glob(".migrate-*"))


@pytest.mark.asyncio
async def test_mcp_parse_422_object_envelope(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_mcp_server(cwd_root, definition={"type": "sse", "url": "https://example.com"})
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "parse"
    assert "Only stdio servers are supported" in detail["message"]


@pytest.mark.asyncio
async def test_mcp_source_not_found_404(client, cwd_root: Path, tmp_path: Path) -> None:
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "missing"
    assert "mcp-servers/pg not found at the source project" in detail["message"]


@pytest.mark.asyncio
async def test_mcp_paused_destination_409(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_mcp_server(cwd_root)
    other = _other_project(tmp_path)
    scope_b = await _register(client, other, enabled=False)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "conflict"
    assert detail["reason_code"] == "sync_paused"
    assert not (other / ".memtomem" / "mcp-servers").exists()


@pytest.mark.asyncio
async def test_mcp_no_store_destination_409(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_mcp_server(cwd_root)
    other = _other_project(tmp_path, store=False)
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "no_memtomem_store"
